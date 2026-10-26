"""ord's witness envelopes, read exactly as ord-litecoin reads them (2026-10-26, for Litecoin's index to match
ord.chikun.market and litescribe.io, which both run stock ynohtna92/ord-litecoin 0.20).

A port of src/inscriptions/envelope.rs, tag.rs and inscription.rs `pointer()` -- the parts that decide what an inscription
IS: which envelopes count, their fields, and the marks that curse one (inscription_updater.rs reads them):

* every input's tapscript (bitcoin's Witness::tapscript: the second-to-last item, the third-to-last after an annex),
  whatever kind of spend it is; a script that does not parse drops every envelope in that input;
* an envelope is OP_FALSE OP_IF "ord" pushes... OP_ENDIF; OP_1NEGATE and OP_1..OP_16 count as pushes but mark it
  `pushnum`; any other opcode, or no OP_ENDIF, and it is no envelope;
* `stutter`: an OP_FALSE that opened nothing, followed by another OP_FALSE, marks the envelope that comes next;
* the pushes before the first empty push at an even place are (tag, value) pairs: an odd count is `incomplete_field`,
  a tag given twice `duplicate_field`, a tag left over after the known ones are taken whose first byte is even
  `unrecognized_even_field`; the body is every push after that empty one, joined.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PROTOCOL_ID = b"ord"

# tag.rs
POINTER, CONTENT_TYPE, PARENT, METADATA, METAPROTOCOL, CONTENT_ENCODING, DELEGATE, RUNE = 2, 1, 3, 5, 7, 9, 11, 13

OP_0, OP_PUSHDATA1, OP_PUSHDATA2, OP_PUSHDATA4, OP_1NEGATE, OP_IF, OP_ENDIF = 0x00, 0x4C, 0x4D, 0x4E, 0x4F, 0x63, 0x68
TAPROOT_ANNEX_PREFIX = 0x50


class ScriptError(ValueError):
    """A script rust-bitcoin's instruction iterator cannot read (a push running past the end)."""


def instructions(script: bytes):
    """(opcode, pushed bytes or None) for each instruction, as rust-bitcoin's `Script::instructions()` reads them:
    OP_0 and the push opcodes give bytes (OP_0 an empty push), every other opcode None. A push past the end raises."""
    at, n = 0, len(script)
    while at < n:
        op = script[at]
        at += 1
        if op == OP_0:
            yield op, b""
            continue
        if op < OP_PUSHDATA1:
            size = op
        elif op in (OP_PUSHDATA1, OP_PUSHDATA2, OP_PUSHDATA4):
            width = {OP_PUSHDATA1: 1, OP_PUSHDATA2: 2, OP_PUSHDATA4: 4}[op]
            if at + width > n:
                raise ScriptError("a push size past the end")
            size = int.from_bytes(script[at:at + width], "little")
            at += width
        else:
            yield op, None
            continue
        if at + size > n:
            raise ScriptError("a push past the end")
        yield op, bytes(script[at:at + size])
        at += size


def tapscript(witness: list[bytes]) -> bytes | None:
    """bitcoin's Witness::tapscript: the second-to-last item, or the third-to-last when the last is an annex."""
    w = list(witness or [])
    if len(w) < 2:
        return None
    if w[-1][:1] == bytes([TAPROOT_ANNEX_PREFIX]):
        return w[-3] if len(w) >= 3 else None
    return w[-2]


@dataclass
class Envelope:
    input: int                      # the input it is in
    offset: int                     # its place among that input's envelopes
    pushnum: bool = False
    stutter: bool = False
    body: bytes | None = None
    content_type: bytes | None = None
    content_encoding: bytes | None = None
    delegate: bytes | None = None
    metadata: bytes | None = None
    metaprotocol: bytes | None = None
    parents: list[bytes] = field(default_factory=list)
    pointer_raw: bytes | None = None
    rune: bytes | None = None
    duplicate_field: bool = False
    incomplete_field: bool = False
    unrecognized_even_field: bool = False

    def pointer(self) -> int | None:
        """inscription.rs `pointer()`: little-endian, nothing but zeros past the eighth byte."""
        value = self.pointer_raw
        if value is None or any(b != 0 for b in value[8:]):
            return None
        return int.from_bytes(bytes(value[:8]).ljust(8, b"\0"), "little")


def _raw_envelopes(script: bytes, input_index: int) -> list[tuple[list[bytes], bool, bool, int]]:
    """envelope.rs `from_tapscript`: (payload pushes, pushnum, stutter, offset) for each envelope; ScriptError when the
    script does not parse (the caller drops the whole input then)."""
    out: list[tuple[list[bytes], bool, bool, int]] = []
    ins = list(instructions(script))          # parse it all first: an error anywhere drops every envelope
    i, stuttered = 0, False
    while i < len(ins):
        op, push = ins[i]
        i += 1
        if push != b"":                                  # an empty push, however written (OP_0, OP_PUSHDATA1 0 ...)
            continue
        # from_instructions: OP_IF, then "ord"
        if i < len(ins) and ins[i] == (OP_IF, None):
            i += 1
            if i < len(ins) and ins[i][1] == PROTOCOL_ID:
                i += 1
                payload: list[bytes] = []
                pushnum, done = False, False
                while i < len(ins):
                    op2, push2 = ins[i]
                    i += 1
                    if op2 == OP_ENDIF and push2 is None:
                        out.append((payload, pushnum, stuttered, len(out)))
                        done = True
                        break
                    if op2 == OP_1NEGATE:
                        pushnum = True
                        payload.append(b"\x81")
                    elif 0x51 <= op2 <= 0x60:
                        pushnum = True
                        payload.append(bytes([op2 - 0x50]))
                    elif push2 is not None:
                        payload.append(push2)
                    else:
                        break                         # any other opcode: no envelope
                if not done:
                    stuttered = False
                continue
            stuttered = i < len(ins) and ins[i][1] == b""
            continue
        stuttered = i < len(ins) and ins[i][1] == b""
    return out


def _parse(payload: list[bytes], input_index: int, offset: int, pushnum: bool, stutter: bool) -> Envelope:
    body_at = next((k for k, p in enumerate(payload) if k % 2 == 0 and p == b""), None)
    head = payload[:body_at] if body_at is not None else payload
    fields: dict[bytes, list[bytes]] = {}
    incomplete = False
    for k in range(0, len(head), 2):
        pair = head[k:k + 2]
        if len(pair) == 2:
            fields.setdefault(pair[0], []).append(pair[1])
        else:
            incomplete = True
    duplicate = any(len(v) > 1 for v in fields.values())

    def take(tag: int) -> bytes | None:
        key = bytes([tag])
        values = fields.get(key)
        if not values:
            return None
        value = values.pop(0)
        if not values:
            fields.pop(key, None)
        return value

    def take_chunked(tag: int) -> bytes | None:
        values = fields.pop(bytes([tag]), None)
        return b"".join(values) if values else None

    env = Envelope(input=input_index, offset=offset, pushnum=pushnum, stutter=stutter,
                   duplicate_field=duplicate, incomplete_field=incomplete)
    env.content_encoding = take(CONTENT_ENCODING)
    env.content_type = take(CONTENT_TYPE)
    env.delegate = take(DELEGATE)
    env.metadata = take_chunked(METADATA)
    env.metaprotocol = take(METAPROTOCOL)
    env.parents = fields.pop(bytes([PARENT]), [])
    env.pointer_raw = take(POINTER)
    env.rune = take(RUNE)
    env.unrecognized_even_field = any(key[:1] and key[0] % 2 == 0 for key in fields)
    env.body = b"".join(payload[body_at + 1:]) if body_at is not None else None
    return env


def envelopes(vin: list[dict]) -> list[Envelope]:
    """envelope.rs `ParsedEnvelope::from_transaction`: every input's envelopes, in input order."""
    out: list[Envelope] = []
    for j, i in enumerate(vin):
        script = tapscript(i.get("witness") or [])
        if script is None:
            continue
        try:
            raw = _raw_envelopes(bytes(script), j)
        except ScriptError:
            continue
        out.extend(_parse(p, j, off, pn, st) for p, pn, st, off in raw)
    return out
