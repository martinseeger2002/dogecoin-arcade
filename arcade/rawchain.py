"""Blocks and transactions read from their bytes, and checked, rather than taken on a node's word.

2026-10-17: an arcade may run only some chains' Cores and borrow the others from arcades that
share theirs over the mesh (arcade/mesh/coreshare.py). A borrowed chain's answers come from somebody
else's node, so the borrowing arcade asks for the RAW block or transaction and reads it here:

* a transaction's id is the hash of its bytes, so a raw transaction that hashes to the id asked for is
  that transaction -- nothing in it can have been changed;
* a block's hash is the hash of its 80-byte header, and the header commits to every transaction in the
  block through the merkle root, so a raw block whose header hashes to the block asked for, and whose
  transactions hash up to its merkle root, is that block;
* the header carries proof of work: scrypt of the header (Pepecoin testnet, Litecoin), or of a merge-
  mined parent's header that commits to this block (AuxPoW: Dogecoin and Pepecoin mainnet). Making a
  block that passes costs real mining at the chain's difficulty.

The decoded form is the one Core prints (`getblock h 2`, `getrawtransaction t 1`), built here from the
bytes, so every caller in the arcade reads a borrowed chain exactly as it reads its own node.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .script import (OutputType, b58check_encode, classify_script, hash160,
                     segwit_encode as segwit_address, witness_program)

AUXPOW_FLAG = 0x100
#: The merged-mining marker in a parent coinbase, before the chain merkle root (auxpow.cpp).
MM_HEADER = bytes.fromhex("fabe6d6d")


class RawError(ValueError):
    """Bytes that are not what they were said to be."""


class Unreadable(RawError):
    """Bytes that may well be honest but that this reader cannot prove (2026-10-18: a Litecoin transaction that is
    wholly MWEB, whose id is its kernel's). Said as an error, and not held against whoever sent them."""


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def scrypt_hash(header: bytes) -> bytes:
    """Litecoin's proof-of-work hash: scrypt(N=1024, r=1, p=1) of the header, salted with itself."""
    return hashlib.scrypt(header, salt=header, n=1024, r=1, p=1, dklen=32)


def target_of(bits: int) -> int:
    size, word = bits >> 24, bits & 0x007FFFFF
    return word >> (8 * (3 - size)) if size <= 3 else word << (8 * (size - 3))


class _Reader:
    def __init__(self, data: bytes, at: int = 0):
        self.data, self.at = data, at

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise RawError("the bytes end early")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def i32(self) -> int:
        return int.from_bytes(self.take(4), "little", signed=True)

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def varint(self) -> int:
        first = self.take(1)[0]
        if first < 0xFD:
            return first
        return int.from_bytes(self.take({0xFD: 2, 0xFE: 4, 0xFF: 8}[first]), "little")

    def blob(self) -> bytes:
        return self.take(self.varint())


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


# --------------------------------------------------------------------------- scripts as Core prints them

_OPS = {0x00: "0", 0x4F: "-1", 0x61: "OP_NOP", 0x63: "OP_IF", 0x64: "OP_NOTIF", 0x67: "OP_ELSE", 0x68: "OP_ENDIF",
        0x69: "OP_VERIFY", 0x6A: "OP_RETURN", 0x75: "OP_DROP", 0x76: "OP_DUP", 0x82: "OP_SIZE", 0x87: "OP_EQUAL",
        0x88: "OP_EQUALVERIFY", 0xA6: "OP_RIPEMD160", 0xA7: "OP_SHA1", 0xA8: "OP_SHA256", 0xA9: "OP_HASH160",
        0xAA: "OP_HASH256", 0xAC: "OP_CHECKSIG", 0xAD: "OP_CHECKSIGVERIFY", 0xAE: "OP_CHECKMULTISIG",
        0xAF: "OP_CHECKMULTISIGVERIFY", 0xB1: "OP_CHECKLOCKTIMEVERIFY", 0xB2: "OP_CHECKSEQUENCEVERIFY",
        0x7C: "OP_SWAP", 0x6B: "OP_TOALTSTACK", 0x6C: "OP_FROMALTSTACK", 0x6D: "OP_2DROP", 0x6E: "OP_2DUP",
        0x73: "OP_IFDUP", 0x74: "OP_DEPTH", 0x77: "OP_NIP", 0x78: "OP_OVER", 0x7A: "OP_ROLL", 0x7B: "OP_ROT",
        0x9A: "OP_BOOLAND", 0x9C: "OP_NUMEQUAL", 0xA0: "OP_GREATERTHAN", 0x9F: "OP_LESSTHAN"}
_SIGHASH = {1: "ALL", 2: "NONE", 3: "SINGLE", 0x81: "ALL|ANYONECANPAY", 0x82: "NONE|ANYONECANPAY",
            0x83: "SINGLE|ANYONECANPAY"}


def _looks_like_signature(push: bytes) -> bool:
    return 9 <= len(push) <= 73 and push[0] == 0x30 and push[1] == len(push) - 3 and push[-1] in _SIGHASH


def asm(script: bytes, signatures: bool = False) -> str:
    """Core's disassembly: pushes as hex (a signature in an input with its [ALL]), small numbers as
    numbers, opcodes by name. An unparseable tail is "[error]", as Core prints it."""
    out, at = [], 0
    while at < len(script):
        op = script[at]
        at += 1
        if 0 < op <= 0x4E:
            if op < 0x4C:
                size = op
            else:
                width = {0x4C: 1, 0x4D: 2, 0x4E: 4}[op]
                if at + width > len(script):
                    out.append("[error]")
                    break
                size = int.from_bytes(script[at:at + width], "little")
                at += width
            if at + size > len(script):
                out.append("[error]")
                break
            push = script[at:at + size]
            at += size
            if signatures and _looks_like_signature(push):
                out.append(push[:-1].hex() + f"[{_SIGHASH[push[-1]]}]")
            elif size <= 4:
                out.append(str(int.from_bytes(push, "little", signed=False) if not push or push[-1] < 0x80
                               else -int.from_bytes(push[:-1] + bytes([push[-1] & 0x7F]), "little")))
            else:
                out.append(push.hex())
        elif 0x51 <= op <= 0x60:
            out.append(str(op - 0x50))
        else:
            out.append(_OPS.get(op, f"OP_UNKNOWN_{op:02x}"))
    return " ".join(out)


# bech32 / segwit encoding lives in arcade/script.py now, the one home for
# address and script primitives: `segwit_address` here is that module's
# `segwit_encode` (imported above), and `witness_program` is read from there
# too -- the duplicate copies that used to sit here are gone.


def script_pubkey(script: bytes, params) -> dict[str, Any]:
    """`scriptPubKey` as Core prints it: asm, hex, type, and the addresses it pays."""
    hrp = getattr(params, "bech32_hrp", "")
    program = witness_program(script) if hrp else None
    if program is not None:
        version, prog = program
        kind = ("witness_v0_keyhash" if version == 0 and len(prog) == 20 else
                "witness_v0_scripthash" if version == 0 and len(prog) == 32 else
                "witness_v1_taproot" if version == 1 and len(prog) == 32 else "witness_unknown")
        said = {"asm": asm(script), "hex": script.hex(), "type": kind}
        if kind != "witness_unknown" or version:
            said.update(reqSigs=1, addresses=[segwit_address(hrp, version, prog)])
        if kind == "witness_unknown" and version == 0:
            said.pop("reqSigs", None)
        return said
    try:
        kind, address, _data, keys, required = classify_script(script, params)
    except Exception:                                   # noqa: BLE001 -- odd scripts are nonstandard
        kind, address, keys, required = OutputType.NONSTANDARD, None, (), 0
    said: dict[str, Any] = {"asm": asm(script), "hex": script.hex(), "type": kind.value}
    if kind in (OutputType.PUBKEYHASH, OutputType.SCRIPTHASH) and address:
        said.update(reqSigs=1, addresses=[address])
    elif kind == OutputType.PUBKEY and keys:
        said.update(reqSigs=1, addresses=[b58check_encode(params.pubkeyhash_version, hash160(keys[0]))])
    elif kind == OutputType.MULTISIG and keys:
        said.update(reqSigs=int(required or 1),
                    addresses=[b58check_encode(params.pubkeyhash_version, hash160(k)) for k in keys])
    return said


# --------------------------------------------------------------------------- transactions

@dataclass
class Tx:
    txid: str
    wtxid: str
    raw: bytes                    # as given, witness and all
    version: int
    locktime: int
    vin: list[dict]               # {txid, vout, script, sequence, witness}; coinbase: txid "" and vout -1
    vout: list[tuple[int, bytes]]  # (koinu, script)
    stripped_size: int

    def verbose(self, params, *, with_hex: bool = True) -> dict[str, Any]:
        """The transaction as Core's `getrawtransaction t 1` prints its own part of it."""
        weight = self.stripped_size * 3 + len(self.raw)
        mweb = getattr(params, "mweb", False)
        ins = []
        for i in self.vin:
            if i["vout"] == -1:
                row = ({"ismweb": False} if mweb else {}) | {"coinbase": i["script"].hex(), "sequence": i["sequence"]}
            else:
                row = ({"ismweb": False} if mweb else {}) | {"txid": i["txid"], "vout": i["vout"],
                       "scriptSig": {"asm": asm(i["script"], signatures=True), "hex": i["script"].hex()},
                       "sequence": i["sequence"]}
            if i["witness"]:
                row["txinwitness"] = [w.hex() for w in i["witness"]]
            ins.append(row)
        said = {"txid": self.txid, "hash": self.wtxid, "version": self.version, "size": len(self.raw),
                "vsize": (weight + 3) // 4, **({"weight": weight} if mweb else {}), "locktime": self.locktime,
                "vin": ins,
                "vout": [({"ismweb": False} if mweb else {}) |
                         {"value": round(v / 100_000_000, 8), "n": n, "scriptPubKey": script_pubkey(s, params)}
                         for n, (v, s) in enumerate(self.vout)]}
        if with_hex:
            said["hex"] = self.raw.hex()
        return said


#: The extended-serialization flag bits: SegWit witnesses, and Litecoin's MWEB part.
FLAG_WITNESS, FLAG_MWEB = 0x01, 0x08


def _read_tx(r: _Reader, alone: bool = False) -> Tx:
    start = r.at
    version = r.i32()
    flags = 0
    if r.data[r.at:r.at + 1] == b"\x00" and r.data[r.at + 1:r.at + 2] not in (b"", b"\x00"):
        flags = r.data[r.at + 1]
        if flags & ~(FLAG_WITNESS | FLAG_MWEB):
            raise RawError(f"a transaction with flags {flags:#04x}")
        r.take(2)
    segwit = bool(flags)
    body_start = r.at
    ins = []
    for _ in range(r.varint()):
        prev = r.take(32)
        index = r.u32()
        script = r.blob()
        sequence = r.u32()
        coinbase = prev == b"\x00" * 32 and index == 0xFFFFFFFF
        ins.append({"txid": "" if coinbase else prev[::-1].hex(), "vout": -1 if coinbase else index,
                    "script": script, "sequence": sequence, "witness": []})
    outs = []
    for _ in range(r.varint()):
        value = r.u64()
        outs.append((value, r.blob()))
    body_end = r.at
    if flags & FLAG_WITNESS:
        for i in ins:
            i["witness"] = [r.blob() for _ in range(r.varint())]
    if flags & FLAG_MWEB:
        # Its MWEB part, an optional: 0 when there is none (the HogEx in every MWEB block). One that is there
        # (a peg-in, 2026-10-18) is not read: a transaction alone ends with its locktime, so the part is passed
        # over whole, and the id is still proved by the ordinary part. Inside a block its end cannot be found.
        if r.take(1) != b"\x00":
            if not alone:
                raise Unreadable("a transaction with an MWEB part inside a block")
            r.at = len(r.data) - 4
    locktime_bytes = r.take(4)
    raw = r.data[start:r.at]
    stripped = r.data[start:start + 4] + r.data[body_start:body_end] + locktime_bytes
    return Tx(txid=sha256d(stripped)[::-1].hex(), wtxid=sha256d(raw)[::-1].hex(), raw=raw, version=version,
              locktime=int.from_bytes(locktime_bytes, "little"), vin=ins, vout=outs, stripped_size=len(stripped))


def read_tx(raw: bytes, txid: str | None = None) -> Tx:
    """A raw transaction, read whole; with `txid`, proved to be that transaction."""
    r = _Reader(raw)
    tx = _read_tx(r, alone=True)
    if r.at != len(raw):
        raise RawError("bytes left over after the transaction")
    if txid is not None and tx.txid != txid and not tx.vin and not tx.vout and len(raw) > 10 and raw[5] & FLAG_MWEB:
        raise Unreadable(f"{txid} is wholly MWEB: its id is its kernel's, which this arcade does not read")
    if txid is not None and tx.txid != txid:
        raise RawError(f"the transaction sent as {txid} hashes to {tx.txid}")
    return tx


# --------------------------------------------------------------------------- blocks

@dataclass
class AuxPow:
    coinbase: Tx
    parent_hash: bytes
    merkle_branch: list[bytes]
    merkle_index: int
    chain_branch: list[bytes]
    chain_index: int
    parent_header: bytes


@dataclass
class Block:
    hash: str
    header: bytes                 # the 80 bytes the hash is of
    version: int
    prev: str
    merkleroot: str
    time: int
    bits: int
    nonce: int
    auxpow: AuxPow | None
    txs: list[Tx] = field(default_factory=list)
    size: int = 0

    def verbose(self, params, *, height: int | None = None, verbosity: int = 2,
                extra: dict | None = None) -> dict[str, Any]:
        """The block as Core's `getblock h 1|2` prints it. Where it sits in the chain (height,
        confirmations, chainwork, nextblockhash, mediantime) is not in the bytes: `extra` brings it."""
        said: dict[str, Any] = {
            "hash": self.hash, "size": self.size, "version": self.version, "versionHex": f"{self.version & 0xFFFFFFFF:08x}",
            "merkleroot": self.merkleroot, "time": self.time, "nonce": self.nonce, "bits": f"{self.bits:08x}",
            "difficulty": _difficulty(self.bits), "previousblockhash": self.prev,
            "tx": [t.verbose(params, with_hex=False) for t in self.txs] if verbosity >= 2 else [t.txid for t in self.txs],
        }
        stripped = self.size - sum(len(t.raw) - t.stripped_size for t in self.txs)
        said.update(strippedsize=stripped, weight=stripped * 3 + self.size)
        if getattr(params, "mweb", False):
            said["nTx"] = len(self.txs)
        if height is not None:
            said["height"] = height
        if self.prev == "00" * 32:
            said.pop("previousblockhash")
        for key, value in (extra or {}).items():
            if key in ("height", "confirmations", "chainwork", "nextblockhash", "mediantime"):
                said[key] = value
        return said


def _difficulty(bits: int) -> float:
    """Core's GetDifficulty, step for step, so the float comes out the same to the last digit."""
    shift, word = (bits >> 24) & 0xFF, bits & 0x00FFFFFF
    if not word:
        return 0.0
    diff = 0x0000FFFF / word
    while shift < 29:
        diff *= 256.0
        shift += 1
    while shift > 29:
        diff /= 256.0
        shift -= 1
    return float(f"{diff:.16g}")                # Core's JSON prints 16 significant digits


def _read_branch(r: _Reader) -> tuple[list[bytes], int]:
    hashes = [r.take(32) for _ in range(r.varint())]
    return hashes, r.i32()


def _read_header(r: _Reader) -> tuple[bytes, AuxPow | None]:
    header = r.take(80)
    version = int.from_bytes(header[:4], "little")
    aux = None
    if version & AUXPOW_FLAG:
        coinbase = _read_tx(r)
        parent_hash = r.take(32)
        branch, index = _read_branch(r)
        chain_branch, chain_index = _read_branch(r)
        aux = AuxPow(coinbase, parent_hash, branch, index, chain_branch, chain_index, r.take(80))
    return header, aux


def _header_fields(header: bytes, aux: AuxPow | None) -> dict:
    return dict(hash=sha256d(header)[::-1].hex(), header=header,
                version=int.from_bytes(header[:4], "little", signed=True), prev=header[4:36][::-1].hex(),
                merkleroot=header[36:68][::-1].hex(), time=int.from_bytes(header[68:72], "little"),
                bits=int.from_bytes(header[72:76], "little"), nonce=int.from_bytes(header[76:80], "little"),
                auxpow=aux)


def read_header(raw: bytes, block_hash: str | None = None) -> Block:
    """A raw header (`getblockheader h false`: 80 bytes, then its AuxPoW when merge-mined)."""
    r = _Reader(raw)
    header, aux = _read_header(r)
    if r.at != len(raw):
        raise RawError("bytes left over after the header")
    block = Block(**_header_fields(header, aux))
    if block_hash is not None and block.hash != block_hash:
        raise RawError(f"the header sent as {block_hash} hashes to {block.hash}")
    return block


def merkle_root(txids: list[str]) -> str:
    level = [bytes.fromhex(t)[::-1] for t in txids]
    if not level:
        return "00" * 32
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256d(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0][::-1].hex()


def read_block(raw: bytes, block_hash: str | None = None, mweb: bool = False) -> Block:
    """A raw block (`getblock h 0`), read whole and proved to be one block: its transactions hash up to
    the merkle root its header carries, and (with `block_hash`) that header is the block asked for. On an
    `mweb` chain (Litecoin) what follows the transactions is the block's MWEB extension, which the arcade
    never reads: the merkle root is of the ordinary transactions, and the HogEx among them commits to it."""
    r = _Reader(raw)
    header, aux = _read_header(r)
    txs = [_read_tx(r) for _ in range(r.varint())]
    if r.at != len(raw) and not mweb:
        raise RawError("bytes left over after the block")
    block = Block(**_header_fields(header, aux), txs=txs, size=len(raw))
    if block_hash is not None and block.hash != block_hash:
        raise RawError(f"the block sent as {block_hash} hashes to {block.hash}")
    if merkle_root([t.txid for t in txs]) != block.merkleroot:
        raise RawError(f"block {block.hash}: its transactions do not make its merkle root")
    return block


# --------------------------------------------------------------------------- proof of work

def _branch_root(leaf: bytes, branch: list[bytes], index: int) -> bytes:
    h = leaf
    for sibling in branch:
        h = sha256d(sibling + h) if index & 1 else sha256d(h + sibling)
        index >>= 1
    return h


def pow_hash(header: bytes, algo: str = "scrypt") -> int:
    """The proof-of-work hash of an 80-byte header as a number: scrypt (Litecoin's), or sha256d (Bitcoin's,
    and the SHA-256 merge-mined coins')."""
    if algo == "sha256d":
        return int.from_bytes(sha256d(header), "little")
    if algo == "scrypt":
        return int.from_bytes(scrypt_hash(header), "little")
    raise RawError(f"no proof-of-work hash called {algo!r}")


def check_work(block: Block, chain_id: int | None = None, algo: str = "scrypt") -> None:
    """Raise unless the header carries the work its bits ask for.

    Plain: scrypt(header) <= target. Merge-mined (AuxPoW, auxpow.cpp CAuxPow::check): the parent's
    coinbase is in the parent block (its merkle branch reaches the parent header's merkle root), that
    coinbase names the root of a chain merkle tree this block's hash is a leaf of (after fabe6d6d), and
    the PARENT header's scrypt meets this block's target. `chain_id`, when given, must be the one in the
    version's top half, so another merge-mined coin's block cannot be passed off as this chain's. `algo`
    is the chain's proof-of-work hash (Params.pow_algo; "scrypt" above stands for it); "" -- a chain whose
    work cannot be checked here, such as a multi-algorithm one -- checks nothing (the caller still proves
    hash, merkle root and links)."""
    if not algo:
        return
    target = target_of(block.bits)
    if target <= 0:
        raise RawError(f"block {block.hash}: bits {block.bits:08x} name no target")
    aux = block.auxpow
    version = block.version & 0xFFFFFFFF
    if aux is None:
        if version & AUXPOW_FLAG:
            raise RawError(f"block {block.hash}: says it is merge-mined and carries no AuxPoW")
        if pow_hash(block.header, algo) > target:
            raise RawError(f"block {block.hash}: its work does not meet its target")
        return
    if chain_id is not None and (version >> 16) != chain_id:
        raise RawError(f"block {block.hash}: chain id {version >> 16}, not {chain_id}")
    if aux.merkle_index != 0:
        raise RawError(f"block {block.hash}: the AuxPoW coinbase is not the parent's first transaction")
    parent_root = aux.parent_header[36:68]
    if _branch_root(bytes.fromhex(aux.coinbase.txid)[::-1], aux.merkle_branch, aux.merkle_index) != parent_root:
        raise RawError(f"block {block.hash}: the AuxPoW coinbase is not in its parent block")
    if len(aux.chain_branch) > 30:
        raise RawError(f"block {block.hash}: the chain merkle branch is too long")
    chain_root = _branch_root(bytes.fromhex(block.hash)[::-1], aux.chain_branch, aux.chain_index)[::-1]
    script = aux.coinbase.vin[0]["script"] if aux.coinbase.vin else b""
    at = script.find(MM_HEADER + chain_root)
    if at < 0:
        # Old merged mining wrote the root with no marker, within the first 20 bytes.
        at = script.find(chain_root)
        if at < 0 or at > 20 or script.find(MM_HEADER) >= 0:
            raise RawError(f"block {block.hash}: the parent coinbase does not commit to this block")
    if pow_hash(aux.parent_header, algo) > target:
        raise RawError(f"block {block.hash}: its parent's work does not meet its target")


#: How much easier one block's target may be than the block before it, at most. Dogecoin's DigiShield
#: retargets every block within -25%..+50% of the timespan; Litecoin and Pepecoin testnet retarget less
#: often and by at most 4x. A borrowed chain whose difficulty falls faster than this is refused: a
#: sharer cannot walk the difficulty down to where faking blocks is cheap.
MAX_EASIER = 4


def too_easy(bits: int, before_bits: int, params=None) -> bool:
    """A target more than MAX_EASIER times easier than the block before's -- except, on a test network,
    a block at the network's easiest difficulty, which its rules allow after a long gap."""
    if params is not None and getattr(params, "min_difficulty_blocks", False) \
            and target_of(bits) <= target_of(params.pow_limit_bits):
        return False
    return target_of(bits) > target_of(before_bits) * MAX_EASIER


def check_link(block: Block, before: Block | None, params=None) -> None:
    """Raise unless `block` follows `before` (the block under it, already checked): it names it, and its
    target is not more than MAX_EASIER times easier (see too_easy)."""
    if before is None:
        return
    if block.prev != before.hash:
        raise RawError(f"block {block.hash} does not follow {before.hash}")
    if too_easy(block.bits, before.bits, params):
        raise RawError(f"block {block.hash}: its difficulty fell more than {MAX_EASIER}x in one block")
