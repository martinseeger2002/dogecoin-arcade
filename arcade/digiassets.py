"""DigiAssets on DigiByte, read for the inscription guard (2026-11-09, security review): which outputs of a transaction
may carry a DigiAsset, so an ordinary send, a module's spend or a Gather never spends one with the DGB under it.

The rules are DigiAsset Core's (github.com/DigiAsset-Core/DigiAsset_Core: DigiAsset::decodeAssetTxHeader,
DigiByteTransaction::decodeAssetTX and decodeAssetTransfer, BitIO::getFixedPrecision), read from this transaction's own
bytes alone:

- **The header.** The transaction's OP_RETURN, one data push, starts "DA" (0x4441), then a version byte and an opcode
  byte. A transaction with no such OP_RETURN moves no asset to any output (DigiAsset Core gives a plain transaction's
  outputs nothing: assets on its inputs are burnt), so it marks nothing.
- **Issuance**, opcodes 1 to 15: version 1 or 2 with opcode 1 or 2 has a 20-byte hash first, opcodes 1, 3 and 4 a
  32-byte metadata hash; then the amount (a fixed-precision number); opcodes 3 and 4 then carry rules; then transfer
  instructions; the last byte is the issuance flags.
- **Transfer** 0x15 and **burn** 0x25: transfer instructions to the end.
- **A transfer instruction:** skip (1 bit), range (1 bit), percent (1 bit), then the output: 13 bits with range, else
  5; then the amount: one byte with percent, else a fixed-precision number. A range pays every output from 0 to the one
  named; a burn's output 31 without range pays none. Whatever is left over goes to the LAST output.

What is marked does not depend on what the inputs carry (that would mean reading every input's history): every output
an instruction names, and the last output, may hold an asset. That is more than DigiAsset Core would give when an
instruction fails or an input holds nothing, never less. Anything not read exactly -- an unknown version or opcode,
rules (opcodes 3 and 4, whose layout is not read here), instructions that run past the data, more than one OP_RETURN
beside a "DA" one -- marks every output that is not an OP_RETURN: fail closed.
"""
from __future__ import annotations

import re

#: The chains whose guard reads DigiAssets (arcade/doginals.Doginals `digiassets`).
CHAINS = frozenset({"dgb", "dgbtest"})
#: The "DA" marker at the start of the OP_RETURN's data.
MARKER = b"DA"
#: The versions DigiAsset Core has written (1, 2, 3); any other is not read, and fails closed.
VERSIONS = frozenset({1, 2, 3})
TRANSFER, BURN = 0x15, 0x25
OP_RETURN = 0x6A
#: What a mark says: an output a read instruction (or the leftover rule) pays, or one marked because the transaction
#: could not be read exactly.
ISSUANCE, TRANSFERRED, BURNT, UNREADABLE = "issuance", "transfer", "burn", "unreadable"


class Unreadable(ValueError):
    """A "DA" transaction this reader cannot follow exactly: every output of it is marked."""


class _Bits:
    """DigiAsset Core's BitIO, as far as the reader needs it: big-endian bits from the start of the data."""

    def __init__(self, data: bytes):
        self.value, self.size, self.pos = int.from_bytes(data, "big"), len(data) * 8, 0

    def left(self) -> int:
        return self.size - self.pos

    def take(self, n: int) -> int:
        if n > self.left():
            raise Unreadable("the data ends inside a field")
        self.pos += n
        return (self.value >> (self.size - self.pos)) & ((1 << n) - 1)

    def number(self) -> int:
        """A fixed-precision number (BitIO::getFixedPrecision): 3 bits of length, 1 to 7 bytes in all; a mantissa and
        a power of ten."""
        length = self.take(3) + 1
        if length >= 7:                 # 7 bytes: the length is the first 2 bits only
            self.pos -= 1
            length = 7
        if length == 1:
            return self.take(5)
        if length < 5:
            mantissa = self.take(length * 8 - 7)
            return mantissa * 10 ** self.take(4)
        if length < 7:
            mantissa = self.take(length * 8 - 6)
            return mantissa * 10 ** self.take(3)
        return self.take(54)


def _data_at(script: bytes) -> int | None:
    """Where the first push's data starts in an OP_RETURN script, or None when it is not OP_RETURN and a push."""
    if len(script) < 2 or script[0] != OP_RETURN:
        return None
    op = script[1]
    if 1 <= op <= 75:
        return 2
    return {76: 3, 77: 4, 78: 6}.get(op)


def _payload(script: bytes) -> bytes:
    """The OP_RETURN's pushed data, all of it there (DigiAsset Core's copyBitcoinData)."""
    at = _data_at(script)
    if at is None:
        raise Unreadable("not an OP_RETURN and a push")
    size = script[1] if script[1] <= 75 else int.from_bytes(script[2:at], "little")
    data = script[at:at + size]
    if len(data) != size:
        raise Unreadable("the OP_RETURN's push runs past the script")
    return data


def says_digiasset(script: bytes) -> bool:
    """Whether an output's script is an OP_RETURN whose data starts "DA" (complete or not)."""
    at = _data_at(script)
    return at is not None and script[at:at + 2] == MARKER


def _instructions(bits: _Bits, footer: int, burn: bool, outputs: int) -> set[int]:
    """The outputs a list of transfer instructions pays, reading to `footer` bits from the end (decodeAssetTransfer)."""
    paid: set[int] = set()
    while bits.left() > footer:
        bits.take(1)                    # skip: moves on to the next input, not to another output
        ranged = bits.take(1)
        percent = bits.take(1)
        output = bits.take(13 if ranged else 5)
        if percent:
            bits.take(8)
        else:
            bits.number()
        if burn and not ranged and output == 31:
            continue                    # burnt: no output gets it
        paid.update(range(0 if ranged else output, min(output, outputs - 1) + 1))
    if bits.left() != footer:
        raise Unreadable("the transfer instructions run into the issuance flags")
    return paid


def _read(data: bytes, outputs: int) -> tuple[str, set[int]]:
    bits = _Bits(data)
    if bits.take(16) != int.from_bytes(MARKER, "big"):
        raise Unreadable("not a DigiAsset header")
    version, opcode = bits.take(8), bits.take(8)
    if version not in VERSIONS:
        raise Unreadable(f"DigiAsset version {version}")
    if 1 <= opcode <= 15:
        if version < 3 and opcode < 3:
            bits.take(160)              # a SHA-1 torrent hash, unused
        if opcode in (1, 3, 4):
            bits.take(256)              # the metadata's SHA-256
        bits.number()                   # the amount issued
        if opcode in (3, 4):
            raise Unreadable("an issuance with rules")
        if bits.left() < 8:
            raise Unreadable("an issuance without its flags byte")
        kind, paid = ISSUANCE, _instructions(bits, 8, False, outputs)
    elif opcode in (TRANSFER, BURN):
        kind, paid = (BURNT if opcode == BURN else TRANSFERRED), _instructions(bits, 0, opcode == BURN, outputs)
    else:
        raise Unreadable(f"DigiAsset opcode {opcode:#04x}")
    return kind, paid | {outputs - 1}  # what is left over goes to the last output


def asset_outputs(scripts: list[bytes]) -> dict[int, str]:
    """{output: why} for each output of a transaction (its scriptPubKeys, in order) that may carry a DigiAsset after
    it; {} for a transaction that is not a DigiAsset one. An OP_RETURN is never marked (it holds no coin)."""
    nulldata = [n for n, s in enumerate(scripts) if s[:1] == bytes([OP_RETURN])]
    if not any(says_digiasset(scripts[n]) for n in nulldata):
        return {}
    coins = [n for n in range(len(scripts)) if n not in nulldata]
    try:
        if len(nulldata) != 1:
            raise Unreadable("more than one OP_RETURN beside a DigiAsset one")
        kind, paid = _read(_payload(scripts[nulldata[0]]), len(scripts))
    except Unreadable:
        return {n: UNREADABLE for n in coins}
    return {n: kind for n in sorted(paid) if n in coins}


def asset_outputs_of(tx: dict) -> dict[int, str]:
    """`asset_outputs` for a transaction as the node decodes it (getrawtransaction, verbose)."""
    outs = sorted(tx.get("vout") or [], key=lambda o: int(o["n"]))
    if [int(o["n"]) for o in outs] != list(range(len(outs))):
        raise ValueError("the node's outputs are not numbered 0, 1, 2 ...")
    return asset_outputs([bytes.fromhex((o.get("scriptPubKey") or {}).get("hex") or "") for o in outs])


#: An OP_RETURN with a push whose data starts "DA", anywhere in a raw transaction: may be a DigiAsset one.
_IN_RAW = re.compile(rb"\x6a(?:[\x01-\x4b]|\x4c[\s\S]|\x4d[\s\S]{2}|\x4e[\s\S]{4})DA")


def maybe_in_raw(raw: bytes) -> bool:
    """Whether a raw transaction may hold a DigiAsset OP_RETURN (a byte search: it may say yes to a transaction that
    does not, never no to one that does)."""
    return _IN_RAW.search(raw) is not None
