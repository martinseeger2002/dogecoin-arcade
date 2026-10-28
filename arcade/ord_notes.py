"""Spend notes: a chunk of blocks, cut down to what the ord walk reads (2026-10-20: "each assigned a batch of
blocks to scan so that no 2 nodes are scanning the same blocks"; "the next one who finishes its chunk would just be
assigned the next chunk in line").

Who owns an inscription now, and every DRC-20 / PRC-20 balance, needs every block in order: a block can spend a coin
an earlier chunk inscribed. So an arcade that scans a chunk writes its NOTES -- every transaction's id, the coins it
spends, its outputs (value and owner), and input 0's script where it can open or continue an envelope -- and the walk
(doginal_index.Walker) applies notes in order exactly as it applies blocks read from a Core. Scanning, the slow part
(reading and decoding every block), is shared; stitching the notes together is quick and reads no chain.

What a note keeps, and why that is enough (doginal_index.Walker.walk_block reads no more):

* the block's 80-byte header: its hash and its previous block's, and the merkle root the transaction ids must hash to
  (`decode` proves that, so the list of transactions is the block's own);
* per transaction: its id, every input's outpoint, every output's value and owner;
* input 0's script only when it can matter to ord_rules.parse -- push-only, and starting with "ord" or a piece count
  (as the chain's dialect reads pushes: on Pepecoin OP_PUSHDATA2/4 are read right, rules 4);
* on a Taproot chain, the witness of an input that carries ord's witness envelope.

A chain of envelope pieces that completes inside the chunk and whose content cannot matter (no "p", no token
operation, no name claim) has its big pushes zeroed: the same lengths, so every size and number stays the same, and a
run of zeros costs nothing compressed. A tag the walk reads after the countdown (a delegate, a parent: rules 4) is never
zeroed. Everything else is kept whole.

Notes are canonical: two arcades that scan the same chunk write the same bytes, so `digest` is a second word on the
chunk beside its births hash, and two arcades agreeing on it means the notes are right.
"""
from __future__ import annotations

import hashlib
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from . import ord_rules, rawchain

#: The notes format. Part of every digest, so notes in another format are never compared with these. 2 (2026-10-20):
#: an envelope begun before the chunk is followed back to its first piece, and zeroed when it cannot matter.
VERSION = 2
#: The notes of a Taproot chain (Bellscoin, Litecoin; 2026-10-25): an output may be a SegWit or Taproot program, kept
#: whole (_WITNESS_OWNER: its version, length and program) so its holder is named by its address after a walk from
#: notes as after a walk from a Core; and, where the chain's indexer reads every input (Bellscoin), a later input's
#: script that opens an envelope. Format 2 everywhere else, unchanged.
WITNESS_VERSION = 3
#: How far back (pieces) an envelope is followed to its first piece; further, it is kept whole.
MAX_BACK = 5000
#: An envelope that may matter is parsed again with each piece it gains; past this many pieces it is kept whole instead
#: (2026-10-20: re-parsing hundreds of pieces, each time, held .25's web process for a minute).
MAX_PARSED = 50

_P2PKH, _P2SH, _HASHED, _WITNESS_OWNER = 0, 1, 2, 3
_COINBASE, _SCRIPT, _WITNESS = 1, 2, 4


class NotesError(ValueError):
    """Notes that do not read, or that are not the block they say they are."""


class Hashed(bytes):
    """An output script kept as its hash160 (it pays no address): Walker.owner_of names it "script:<hash>" as it
    names the whole script."""


@dataclass
class NoteTx:
    txid: str
    vin: list[dict]
    vout: list[tuple[int, bytes]]


@dataclass
class NoteBlock:
    hash: str
    prev: str
    header: bytes
    txs: list[NoteTx] = field(default_factory=list)


# --------------------------------------------------------------------------- what can matter

def may_envelope(script: bytes, dialect: str = "dogecoin") -> bool:
    """Can input 0's script open or continue an envelope (ord_rules.parse): push-only, two pushes or more, the first
    "ord" or a piece count? A plain signature (a 72-byte push first) cannot. Says yes to everything the walk can read
    (rules 4, checked): a Pepecoin envelope of no pieces ("ord" first) and a last piece carrying tags (a piece count
    first). A transaction carrying ONLY tags never matters: the official indexers read a chain's tags only from the
    script its countdown ends in (ord_rules.read), and a script after that starts nothing."""
    pushes = ord_rules.decode_push_datas(script, dialect)
    if pushes is None or len(pushes) < 2:
        return False
    return pushes[0] == b"ord" or ord_rules.push_number(pushes[0]) is not None


def content_matters(ctype: bytes, body: bytes) -> bool:
    """Does the walk read this content, beyond its length: a "p", a token operation on either protocol, a name?"""
    return (ord_rules.protocol_of(body) is not None
            or ord_rules.token_op("drc-20", ctype, body) is not None
            or ord_rules.token_op("prc-20", ctype, body) is not None
            or ord_rules.name_claim(ctype, body) is not None)


def may_matter(ctype: bytes, first: bytes) -> bool:
    """Can an envelope's content matter, known from its first piece alone: plain text or JSON (a name), or a body that
    may start with "{" (a "p"). Exact the other way: when this is False, name_claim and protocol_of are None for any
    body, and a token is never more than one piece."""
    t = ctype.decode("utf-8", "replace").split(";")[0].strip().lower()
    if t in ("text/plain", "application/json"):
        return True
    head = first.lstrip()[:1]
    return not head or head == b"{"


def _first_piece(script: bytes, dialect: str = "dogecoin") -> tuple[bytes, bytes] | None:
    """(content type, its first data) of a genesis piece, or None when it is not one."""
    pushes = ord_rules.decode_push_datas(script, dialect)
    if not pushes or pushes[0] != b"ord" or len(pushes) < 3:
        return None
    return pushes[2], (pushes[4] if len(pushes) > 4 else b"")


def _multisig_spend(script: bytes, dialect: str = "dogecoin") -> bool:
    """An ordinary P2SH multisig spend, by its script alone: OP_0, a DER signature, ..., a redeem script ending
    OP_CHECKMULTISIG. Such a script passes may_envelope (its first push reads as the number 0) and is no piece of an
    envelope: a Doginal's reveal ends with its own lock script, never with OP_CHECKMULTISIG (2026-10-21: on Dogecoin,
    chains of these spends were followed back 5,000 transactions each, so a chunk's notes were never finished and the
    a test machine's and .25's walks stood still). Only the follow-back below asks; what notes KEEP is decided by may_envelope."""
    pushes = ord_rules.decode_push_datas(script, dialect)
    if not pushes or len(pushes) < 3 or pushes[0] != b"":
        return False
    sig, redeem = pushes[1], pushes[-1]
    return (70 <= len(sig) <= 73 and sig[:1] == b"\x30" and redeem[-1:] == b"\xae")


_GENESIS: dict[str, Any] = {}                  # txid of a piece -> its envelope's first piece (or "none"), kept
#: Pieces kept in _GENESIS at most (2026-10-28: Bellscoin's chunks of early 2024 each carry ~8,400 envelopes begun before
#: them, followed back ~37 pieces each; at 200,000 the first chunk's answers were let go before the next one asked).
GENESIS_KEPT = 500_000


class OutOfTime(Exception):
    """`finish` ran past its deadline: the chunk is not finished yet, and is asked again (what was followed back so
    far is kept in _GENESIS, so the next try goes on from there)."""


def genesis_before(txid: str, lookup, dialect: str = "dogecoin") -> Any:
    """Follow an envelope back from the piece in transaction `txid` (each piece's input 0 spends the one before) to
    its first piece: (content type, first data); "none" when it is no envelope's piece; None when too far back."""
    seen, at = [], txid
    for _ in range(MAX_BACK):
        if at in _GENESIS:
            found = _GENESIS[at]
            break
        tx = rawchain.read_tx(lookup(at), at)
        script = tx.vin[0]["script"] if tx.vin and tx.vin[0]["vout"] != -1 else b""
        seen.append(at)
        if not may_envelope(script, dialect) or _multisig_spend(script, dialect):
            found = "none"
            break
        first = _first_piece(script, dialect)
        if first is not None:
            found = first
            break
        at = tx.vin[0]["txid"]
    else:
        return None
    for t in seen:
        _GENESIS[t] = found
    _trim_genesis()
    return found


def _trim_genesis() -> None:
    while len(_GENESIS) > GENESIS_KEPT:
        _GENESIS.pop(next(iter(_GENESIS)))


def _tag_values(script: bytes, genesis: bool, dialect: str) -> set[int] | None:
    """Which pushes (by position) of a piece's script are the values of tags the walk reads after the countdown -- a
    delegate's, a parent's (ord_rules.DELEGATE_TAG / PARENT_TAG) -- so zeroing keeps them (rules 4). The countdown is
    followed as the walk follows it: a genesis from its piece count, a later piece from its own first number. None
    when the dialect reads the script's pushes otherwise than their opcodes say (wonky-ord's OP_PUSHDATA2/4 quirk):
    such a script is kept whole."""
    pushes = ord_rules.decode_push_datas(script, dialect)
    if pushes is None or pushes != ord_rules.decode_push_datas(script, "pepecoin"):
        return None
    if genesis:
        npieces, i = (ord_rules.push_number(pushes[1]) if len(pushes) > 1 else None), 3
    else:
        first = ord_rules.push_number(pushes[0]) if pushes else None
        npieces, i = (first + 1 if first is not None else None), 0
    if npieces is None:
        return set()
    while npieces and i + 1 < len(pushes) and ord_rules.push_number(pushes[i]) == npieces - 1:
        i, npieces = i + 2, npieces - 1
    if npieces:
        return set()
    keys = {ord_rules.DELEGATE_TAG[dialect], ord_rules.PARENT_TAG.get(dialect)}
    keys = {k.encode() if isinstance(k, str) else k for k in keys if k is not None}
    return {j + 1 for j in range(i, len(pushes) - 1, 2) if pushes[j] in keys}


def _zeroed(script: bytes, genesis: bool, dialect: str = "dogecoin") -> bytes:
    """The same script with every push longer than 8 bytes zeroed (a genesis keeps "ord", the piece count and the
    content type; every piece keeps the tags the walk reads after its countdown, `_tag_values`): the same operations
    and lengths, so parse reads the same pieces, the same sizes and the same delegate and parent."""
    keep_too = _tag_values(script, genesis, dialect)
    if keep_too is None:
        return script
    out, at, n = bytearray(), 0, 0
    while at < len(script):
        op = script[at]
        at += 1
        if 1 <= op <= 75:
            size, head = op, bytes([op])
        elif op == 0x4C:
            size, head = script[at], script[at - 1:at + 1]
            at += 1
        elif op == 0x4D:
            size, head = int.from_bytes(script[at:at + 2], "little"), script[at - 1:at + 2]
            at += 2
        elif op == 0x4E:
            size, head = int.from_bytes(script[at:at + 4], "little"), script[at - 1:at + 4]
            at += 4
        else:
            out.append(op)
            n += 1
            continue
        data = script[at:at + size]
        at += size
        keep = size <= 8 or (genesis and n < 3) or n in keep_too
        out += head + (data if keep else bytes(len(data)))
        n += 1
    return bytes(out)


# --------------------------------------------------------------------------- writing

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _blob(b: bytes) -> bytes:
    return _varint(len(b)) + b


def witness_program(script: bytes) -> tuple[int, bytes] | None:
    """(version, program) of a SegWit v0 (20 or 32 bytes) or Taproot (v1, 32 bytes) output, else None -- the outputs
    named by a bech32 / bech32m address (BIP 141, BIP 341)."""
    if len(script) == 22 and script[:2] == b"\x00\x14" or len(script) == 34 and script[:2] == b"\x00\x20":
        return 0, bytes(script[2:])
    if len(script) == 34 and script[:2] == b"\x51\x20":
        return 1, bytes(script[2:])
    return None


def _owner_bytes(script: bytes, witness: bool = False) -> bytes:
    from .script import hash160
    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
        return bytes([_P2PKH]) + script[3:23]
    if len(script) == 23 and script[:2] == b"\xa9\x14" and script[22:] == b"\x87":
        return bytes([_P2SH]) + script[2:22]
    program = witness_program(script) if witness else None
    if program is not None:
        return bytes([_WITNESS_OWNER, program[0], len(program[1])]) + program[1]
    return bytes([_HASHED]) + hash160(script)


def block_notes(block, taproot: bool = False, dialect: str = "dogecoin", every_input: bool = False) -> list:
    """One block's notes, before the chunk is finished: [header, [(txid, ins, outs)...]] with input 0's script kept
    whole where it may matter (the chunk's finish decides what is zeroed)."""
    txs = []
    for tx in block.txs:
        ins = []
        for j, i in enumerate(tx.vin):
            coinbase = i["vout"] == -1
            script = i["script"] if (j == 0 and not coinbase and may_envelope(i["script"], dialect)) else None
            if j and every_input and not coinbase and i["script"][:4] == b"\x03ord":
                script = i["script"]                     # a later input's envelope (Bellscoin), kept whole
            witness = None
            if taproot and not coinbase and i.get("witness"):
                from .doginal_index import taproot_envelopes
                if taproot_envelopes(i["witness"]):
                    witness = list(i["witness"])
            ins.append((None if coinbase else i["txid"], i["vout"], script, witness))
        txs.append((tx.txid, ins, [(v, _owner_bytes(s, taproot)) for v, s in tx.vout]))
    return [block.header, txs]


def finish(blocks: list[bytes], lookup=None, dialect: str = "dogecoin", taproot: bool = False,
           deadline: float | None = None) -> bytes:
    """A chunk's notes, canonical: its blocks' notes (`one_block`, in height order) with the content of every envelope
    that cannot matter zeroed. The bytes `digest` and `pack` are of. Every arcade finishes a chunk from the same block
    notes the same way, whether it walked the blocks or helped with them.

    Which envelopes cannot matter: one complete inside the chunk whose content says so (content_matters); and one whose
    first piece says so (may_matter) -- known here when it began here, else by following it back with `lookup(txid)`
    (raw transaction bytes, from a Core: 2026-10-20, PEP's big images in hundreds of pieces cross every chunk's edge,
    and kept whole they made the notes as big as the blocks). Without `lookup`, a piece of an envelope begun before
    the chunk is kept whole. A script zeroed that is no piece at all is never read by the walk anyway.

    `deadline` (time.monotonic()): OutOfTime when it has passed before an envelope is followed back -- never in the
    middle of one, so each one followed is kept in _GENESIS and the next try goes on past it (2026-10-28: Bellscoin's
    walk stood still 14 hours behind one chunk's ~8,400 follow-backs, read a coin at a time from a hard disk). Every
    envelope the chunk carries on is kept in _GENESIS too, so the next chunk follows each back one piece, not to its
    first."""
    parts = [_structure(b, taproot) for b in blocks]
    # Envelope chains inside the chunk, linked as the walk links them: a piece's input 0 spends the piece before it.
    chains: dict[str, dict] = {}                        # txid of the latest piece -> {"pieces", "first", "whole"}
    zero: dict[tuple[int, int], bool] = {}             # (block, tx) -> is it the genesis piece
    for b, (_header, txs) in enumerate(parts):
        for t, (txid, ins, _outs) in enumerate(txs):
            if not ins or ins[0][0] is None or ins[0][2] is None:
                continue
            prev, script = ins[0][0], ins[0][2]
            chain = chains.pop(prev, None)
            first = _first_piece(script, dialect)
            if first is not None:
                # A script opening with "ord" begins an envelope of its own (kept as one: its content type is read).
                # Should the walk read it as the next piece of a chain it spends instead, it reads nothing of it.
                chain = {"pieces": [], "first": first, "whole": True}
            if chain is None:
                if lookup is None or _multisig_spend(script, dialect):
                    continue                            # no way to look, or no piece at all: kept whole
                else:
                    if deadline is not None and time.monotonic() > deadline:
                        raise OutOfTime(f"followed back until the deadline; {len(_GENESIS)} pieces known")
                    found = genesis_before(prev, lookup, dialect)
                    if found is None:
                        continue                        # too far back to follow: kept whole
                    chain = {"pieces": [], "first": found, "whole": False}
            chain["pieces"].append((b, t))
            pieces = chain["pieces"]
            first = chain["first"]
            if first == "none" or not may_matter(*first):
                zero.update({p: chain["whole"] and i == 0 for i, p in enumerate(pieces)})
                chains[txid] = chain
                continue
            if not chain["whole"] or len(pieces) > MAX_PARSED:
                chains[txid] = chain                    # may matter: begun before, or too long to parse again
                continue
            state, ctype, body = ord_rules.parse([parts[pb][1][pt][1][0][2] for pb, pt in pieces], dialect)
            if state == "partial":
                chains[txid] = chain
            elif state == "complete" and not content_matters(ctype, body):
                zero.update({p: i == 0 for i, p in enumerate(pieces)})
    if lookup is not None:
        for txid, chain in chains.items():              # carried on past the chunk: the next one asks from here
            _GENESIS[txid] = chain["first"]
        _trim_genesis()
    return _encode(parts, zero, dialect, WITNESS_VERSION if taproot else VERSION)


def one_block(block, taproot: bool = False, dialect: str = "dogecoin", every_input: bool = False) -> bytes:
    """One block's notes as read (nothing zeroed yet): what a scanner keeps until its chunk is whole."""
    return _encode([block_notes(block, taproot, dialect, every_input)], {})[1:]


def _encode(parts: list, zero: dict, dialect: str = "dogecoin", version: int = VERSION) -> bytes:
    out = bytearray([version])
    out += _varint(len(parts))
    for b, (header, txs) in enumerate(parts):
        out += header + _varint(len(txs))
        for t, (txid, ins, outs) in enumerate(txs):
            out += bytes.fromhex(txid) + _varint(len(ins))
            for j, (ptxid, vout, script, witness) in enumerate(ins):
                flags = (_COINBASE if ptxid is None else 0) | (_SCRIPT if script is not None else 0) | \
                    (_WITNESS if witness is not None else 0)
                out.append(flags)
                if ptxid is not None:
                    out += bytes.fromhex(ptxid) + _varint(vout)
                if script is not None:
                    if j == 0 and (b, t) in zero:
                        script = _zeroed(script, zero[(b, t)], dialect)
                    out += _blob(script)
                if witness is not None:
                    out += _varint(len(witness)) + b"".join(_blob(w) for w in witness)
            out += _varint(len(outs))
            for value, owner in outs:
                out += _varint(value) + owner
    return bytes(out)


def _owner_read(r, programs: bool) -> bytes:
    """One output's owner bytes: 21 in format 2; in format 3 a witness program's own length."""
    kind = r.take(1)
    if programs and kind[0] == _WITNESS_OWNER:
        head = r.take(2)
        return kind + head + r.take(head[1])
    return kind + r.take(20)


def _structure(raw: bytes, programs: bool = False) -> list:
    """`one_block`'s bytes back to [header, [(txid, ins, outs)...]] (outs as their owner bytes)."""
    r = _R(b"\x01" + raw)
    r.take(1)
    if r.varint() != 1:
        raise NotesError("block notes hold one block")
    header = r.take(80)
    txs = []
    for _t in range(r.varint()):
        txid = r.take(32).hex()
        ins = []
        for _i in range(r.varint()):
            flags = r.take(1)[0]
            ptxid, vout = (None, -1) if flags & _COINBASE else (r.take(32).hex(), r.varint())
            script = r.blob() if flags & _SCRIPT else None
            witness = [r.blob() for _w in range(r.varint())] if flags & _WITNESS else None
            ins.append((ptxid, vout, script, witness))
        outs = []
        for _o in range(r.varint()):
            value = r.varint()
            outs.append((value, _owner_read(r, programs)))
        txs.append((txid, ins, outs))
    if r.at != len(r.data):
        raise NotesError("bytes left over after the block notes")
    return [header, txs]


def digest(notes: bytes) -> str:
    return hashlib.sha256(b"ord-notes\n" + notes).hexdigest()


def pack(notes: bytes) -> bytes:
    return zlib.compress(notes, 6)


def unpack(blob: bytes, limit: int = 256 * 1024 * 1024) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(blob, limit)
    except zlib.error as exc:
        raise NotesError(f"notes that do not unpack: {exc}") from None
    if d.unconsumed_tail:
        raise NotesError("notes larger than any chunk's")
    if not d.eof or d.unused_data:
        raise NotesError("notes cut short, or with bytes after them")
    return out


# --------------------------------------------------------------------------- reading

class _R:
    def __init__(self, data: bytes):
        self.data, self.at = data, 0

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise NotesError("notes end too soon")
        got = self.data[self.at:self.at + n]
        self.at += n
        return got

    def varint(self) -> int:
        n = shift = 0
        while True:
            b = self.take(1)[0]
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7
            if shift > 70:
                raise NotesError("a number too long")

    def blob(self) -> bytes:
        return self.take(self.varint())


def _script_of(kind: int, h: bytes) -> bytes:
    if kind == _WITNESS_OWNER:                          # version, length, program
        return (b"\x00" if h[0] == 0 else bytes([0x50 + h[0]])) + bytes([h[1]]) + h[2:]
    if kind == _P2PKH:
        return b"\x76\xa9\x14" + h + b"\x88\xac"
    if kind == _P2SH:
        return b"\xa9\x14" + h + b"\x87"
    if kind == _HASHED:
        return Hashed(h)
    raise NotesError("an output of no kind")


def decode(notes: bytes) -> list[NoteBlock]:
    """The blocks of a chunk's notes, each proved to be the block its header names: the transaction ids hash up to
    the header's merkle root (what is said of each transaction beyond its id is the scanners' word, which is why two of
    them must agree on `digest`)."""
    r = _R(notes)
    version = r.take(1)[0]
    if version not in (VERSION, WITNESS_VERSION):
        raise NotesError("notes in another format")
    blocks = []
    for _ in range(r.varint()):
        header = r.take(80)
        fields = rawchain._header_fields(header, None)
        txs = []
        for _t in range(r.varint()):
            txid = r.take(32).hex()
            vin = []
            for _i in range(r.varint()):
                flags = r.take(1)[0]
                if flags & _COINBASE:
                    i = {"txid": "", "vout": -1, "script": b"", "witness": []}
                else:
                    i = {"txid": r.take(32).hex(), "vout": r.varint(), "script": b"", "witness": []}
                if flags & _SCRIPT:
                    i["script"] = r.blob()
                if flags & _WITNESS:
                    i["witness"] = [r.blob() for _w in range(r.varint())]
                vin.append(i)
            vout = []
            for _o in range(r.varint()):
                value = r.varint()
                owner = _owner_read(r, version == WITNESS_VERSION)
                vout.append((value, _script_of(owner[0], owner[1:])))
            txs.append(NoteTx(txid, vin, vout))
        if not txs or rawchain.merkle_root([t.txid for t in txs]) != fields["merkleroot"]:
            raise NotesError(f"block {fields['hash']}: its transactions do not hash to its merkle root")
        blocks.append(NoteBlock(fields["hash"], fields["prev"], header, txs))
    if r.at != len(notes):
        raise NotesError("bytes left over after the notes")
    return blocks
