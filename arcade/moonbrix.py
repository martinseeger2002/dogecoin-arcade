"""MoonBrix 2 on Kotia (tbuuol 2026-11-10). KOTIA ONLY (Params.moonbrix).

MoonBrix is tbuuol's OP_RETURN protocol on Kotia: TLDs with white- and blacklists, spaces in them, inscriptions,
folders and files, properties (fixed, managed, NFTs) and a DEX that sells them for KOT. The protocol is
`arcade/web/docs/moonbrix.md`; this file does what that page says and nothing else. MoonBrix 2 replaces the C++
prototype (github.com/tbuuol/MoonBrix): tbuuol asked for it to be made clean and robust, and its commands on Kotia
were tests, so nothing of them is kept.

The rules that decide what a command is (the page has them whole):
- A transaction's first OP_RETURN output starts with MAGIC; the data of it and every OP_RETURN output after it,
  joined, is the payload. Every MoonBrix transaction of a block counts, in block order.
- The sender is the address of the coin the first input spends; the target, where one is needed, the first output
  paying an address other than the sender's.
- A command is applied whole or rejected whole, and a rejection goes into the log with its reason. Nothing stops the
  reader but the node itself (a part's data it cannot fetch), and then it tries the block again later.

The state is one JSON document in `<net>-moonbrix.sqlite`, with the document as it was before each block that changed
it, so a reorg is rolled back. MoonBrix commands are rare, so the arcade's unspent set (utxoset.py `data_blocks`) says
which blocks to read and everything else is skipped.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from . import rawchain

VERSION = 2
MAGIC = b"MB" + bytes([VERSION])
#: Where reading starts (the prototype's activation; nothing before it is read).
ACTIVATION = 1_236_675
#: Blocks whose document before them is kept, so a reorg up to this deep is rolled back rather than read again.
UNDO_DEPTH = 200
U64 = 1 << 64
#: Kotia's Core relays an OP_RETURN script of at most 15,000 bytes: OP_RETURN, OP_PUSHDATA2 and its length, the data.
MAX_PUSH = 15_000 - 4
MAX_PARTS = 10_000
#: The chain this reader reads (a `ref` naming it, or naming no chain, points here) and how a chain is named in one.
HOME_CHAIN = "kot"
CHAIN = re.compile(r"[a-z0-9]{2,8}")
#: The most text a `data` field holds inline; more goes into an inscription it points to.
MAX_DATA_TEXT = 1024
#: The longest payment window an offer may give a reservation, in blocks.
MAX_WINDOW = 10_000

WHITELIST, BLACKLIST = 0, 1
FIXED, MANAGED, NFT = 0, 1, 2
KINDS = {FIXED: "fixed", MANAGED: "managed", NFT: "nft"}

#: op -> (name, fields, target). target: None, "need" (rejected without one) or "maybe" (else the sender).
OPS: dict[int, tuple[str, tuple[str, ...], str | None]] = {
    0x01: ("claim tld", ("str", "u8", "str"), None),
    0x02: ("give tld", ("str",), "need"),
    0x03: ("set list mode", ("str", "u8"), None),
    0x04: ("list add", ("str",), "need"),
    0x05: ("list remove", ("str",), "need"),
    0x06: ("list clear", ("str",), None),
    0x10: ("claim space", ("str", "u32"), None),
    0x11: ("give space", ("str", "u32"), "need"),
    0x12: ("name space", ("str", "u32", "str"), None),
    0x20: ("inscribe", ("str", "str", "rest"), None),
    0x21: ("part", ("rest",), None),
    0x22: ("assemble", ("str", "str", "var", "id", "refs"), None),
    0x30: ("make folder", ("str", "u32", "str"), None),
    0x31: ("put file", ("str", "u32", "str", "ref"), None),
    0x32: ("replace file", ("str", "u32", "str", "ref"), None),
    0x33: ("remove", ("str", "u32", "str"), None),
    0x34: ("move", ("str", "u32", "str", "str"), None),
    0x35: ("join", ("str", "u32", "str", "str"), None),
    0x40: ("create property", ("str", "u32", "str", "str", "u8", "u8", "var", "data"), None),
    0x41: ("give property", ("str",), "need"),
    0x42: ("lock property", ("str",), None),
    0x43: ("mint", ("str", "var"), "maybe"),
    0x44: ("send", ("str", "var"), "need"),
    0x45: ("burn", ("str", "var"), None),
    0x46: ("mint nft", ("str", "data"), "maybe"),
    0x47: ("send nft", ("str", "var"), "need"),
    0x48: ("set issuer data", ("str", "var", "data"), None),
    0x49: ("set holder data", ("str", "var", "data"), None),
    0x50: ("offer", ("str", "var", "var", "var", "var"), None),
    0x51: ("cancel", ("id",), None),
    0x52: ("reserve", ("id", "var"), None),
    0x53: ("pay", ("id",), None),
}
CODES = {name: op for op, (name, _f, _t) in OPS.items()}


class MoonBrixError(RuntimeError):
    """The document and the chain disagree, or the node could not give what a command needs: read the block again."""


class Rejected(ValueError):
    """A command that breaks a rule. It changes nothing and is logged with this reason."""


# --- the payload's fields --------------------------------------------------------------------------------------

class _Fields:
    def __init__(self, data: bytes) -> None:
        self.buf, self.at = data, 0

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.buf):
            raise Rejected("the payload ends too early")
        got = self.buf[self.at:self.at + n]
        self.at += n
        return got

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def var(self) -> int:
        value = shift = 0
        while True:
            byte = self.u8()
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
            if shift >= 70:
                raise Rejected("a number past 64 bits")
        if value >= U64:
            raise Rejected("a number past 64 bits")
        if byte == 0 and shift > 7:
            raise Rejected("a number not in its shortest form")
        return value

    def str(self) -> bytes:
        return self.take(self.var())

    def id(self) -> str:
        return self.take(32).hex()

    def ref(self) -> tuple[bytes, str]:
        """(the chain's ticker as written, empty for this chain; a transaction id)."""
        return self.str(), self.id()

    def refs(self) -> list[tuple[bytes, str]]:
        n = self.var()
        if not 1 <= n <= MAX_PARTS:
            raise Rejected(f"a list of {n} pointers (1 to {MAX_PARTS})")
        return [self.ref() for _ in range(n)]

    def data(self) -> tuple[str, Any] | None:
        form = self.u8()
        if form == 0:
            return None
        if form == 1:
            return "text", self.str()
        if form == 2:
            return "ref", self.ref()
        raise Rejected("a data field is 00 (empty), 01 (text) or 02 (a pointer)")

    def rest(self) -> bytes:
        if self.at >= len(self.buf):
            raise Rejected("no data")
        got = self.buf[self.at:]
        self.at = len(self.buf)
        return got


def decode(payload: bytes) -> tuple[int, list[Any]]:
    """(op, its fields) of a payload that starts with MAGIC. Raises Rejected."""
    if len(payload) < len(MAGIC) + 1:
        raise Rejected("no op")
    op = payload[len(MAGIC)]
    if op not in OPS:
        raise Rejected(f"op {op:02x} is not MoonBrix 2's")
    r = _Fields(payload[len(MAGIC) + 1:])
    values = [getattr(r, kind)() for kind in OPS[op][1]]
    if r.at != len(r.buf):
        raise Rejected("bytes after the last field")
    return op, values


def _var(n: int) -> bytes:
    if not 0 <= n < U64:
        raise ValueError("a var is 0 to 2^64-1")
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ref(value: tuple[str, str]) -> bytes:
    chain = value[0].encode()
    return _var(len(chain)) + chain + bytes.fromhex(value[1])


def encode(name: str, *values: Any) -> bytes:
    """The payload of command `name` (OPS) with these fields: str as text or bytes, id as hex, ref as (chain, txid)
    with "" for this chain, refs as a list of them, data as None, text, or a ref. For the arcade's own sends and the
    tests; what it builds is what `decode` reads."""
    op = CODES[name]
    kinds = OPS[op][1]
    if len(values) != len(kinds):
        raise ValueError(f"{name} takes {len(kinds)} fields")
    out = bytearray(MAGIC + bytes([op]))
    for kind, value in zip(kinds, values):
        if kind == "u8":
            out += bytes([value])
        elif kind == "u32":
            out += int(value).to_bytes(4, "big")
        elif kind == "var":
            out += _var(value)
        elif kind == "str":
            raw = value.encode() if isinstance(value, str) else bytes(value)
            out += _var(len(raw)) + raw
        elif kind == "id":
            out += bytes.fromhex(value)
        elif kind == "ref":
            out += _ref(value)
        elif kind == "refs":
            out += _var(len(value)) + b"".join(_ref(v) for v in value)
        elif kind == "data":
            if value is None:
                out += b"\x00"
            elif isinstance(value, tuple):
                out += b"\x02" + _ref(value)
            else:
                raw = value.encode() if isinstance(value, str) else bytes(value)
                out += b"\x01" + _var(len(raw)) + raw
        elif kind == "rest":
            out += bytes(value)
    return bytes(out)


def data_scripts(payload: bytes) -> list[bytes]:
    """The OP_RETURN scripts that carry `payload` on Kotia, each within what its Core relays, in order. A transaction
    that carries n of them needs n outputs that pay an address too (Kotia: at most half the outputs are data)."""
    scripts = []
    for at in range(0, len(payload), MAX_PUSH):
        piece = payload[at:at + MAX_PUSH]
        if len(piece) < 0x4C:
            push = bytes([len(piece)])
        elif len(piece) <= 0xFF:
            push = b"\x4c" + bytes([len(piece)])
        else:
            push = b"\x4d" + len(piece).to_bytes(2, "little")
        scripts.append(b"\x6a" + push + piece)
    return scripts


# --- sending one (the arcade's wallet, moonbrix_web.py) ----------------------------------------------------------

#: The most payload one sent transaction carries: six OP_RETURN outputs hold 89,976 bytes, and what is left of
#: Kotia's 100,000-byte limit is kept for the inputs and outputs. A bigger file goes as parts (21) and an assemble (22).
MAX_SEND = 80_000

#: What a person fills in for each command, in order: (label, input). The browser turns each input into the field
#: OPS names: "tld" text, "mode" whitelist/blacklist, "space" a space's number or name, "tokens" an amount in the
#: property's own units, "kot" KOT (into koinu), "kind" fixed/managed/nft, "file" a file's bytes, "data" text or a
#: pointer (chain:txid), "ref" a pointer, "txid" a transaction id, "path" a path, "int" a number, "text" text.
FORM: dict[str, list[tuple[str, str]]] = {
    "claim tld": [("TLD", "tld"), ("List", "mode"), ("Message", "text")],
    "give tld": [("TLD", "tld")],
    "set list mode": [("TLD", "tld"), ("List", "mode")],
    "list add": [("TLD", "tld")],
    "list remove": [("TLD", "tld")],
    "list clear": [("TLD", "tld")],
    "claim space": [("TLD", "tld"), ("Space number", "space")],
    "give space": [("TLD", "tld"), ("Space", "space")],
    "name space": [("TLD", "tld"), ("Space", "space"), ("Name", "text")],
    "inscribe": [("Type", "text"), ("Name", "text"), ("File", "file")],
    "make folder": [("TLD", "tld"), ("Space", "space"), ("Path", "path")],
    "put file": [("TLD", "tld"), ("Space", "space"), ("Path", "path"), ("Inscription", "ref")],
    "replace file": [("TLD", "tld"), ("Space", "space"), ("Path", "path"), ("Inscription", "ref")],
    "remove": [("TLD", "tld"), ("Space", "space"), ("Path", "path")],
    "move": [("TLD", "tld"), ("Space", "space"), ("From", "path"), ("To", "path")],
    "join": [("TLD", "tld"), ("Space", "space"), ("Folder", "path"), ("Into", "path")],
    "create property": [("TLD", "tld"), ("Space", "space"), ("Ticker", "text"), ("Name", "text"), ("Kind", "kind"),
                        ("Decimals", "int"), ("Supply", "tokens"), ("Grant data", "data")],
    "give property": [("Ticker", "text")],
    "lock property": [("Ticker", "text")],
    "mint": [("Ticker", "text"), ("Amount", "tokens")],
    "send": [("Ticker", "text"), ("Amount", "tokens")],
    "burn": [("Ticker", "text"), ("Amount", "tokens")],
    "mint nft": [("Ticker", "text"), ("Grant data", "data")],
    "send nft": [("Ticker", "text"), ("Item", "int")],
    "set issuer data": [("Ticker", "text"), ("Item", "int"), ("Issuer data", "data")],
    "set holder data": [("Ticker", "text"), ("Item", "int"), ("Holder data", "data")],
    "offer": [("Ticker", "text"), ("Amount or item", "tokens"), ("Price", "kot"), ("Blocks to pay", "int"),
              ("Deposit", "kot")],
    "cancel": [("Offer", "txid")],
    "reserve": [("Offer", "txid"), ("Amount", "tokens")],
    "pay": [("Reservation", "txid")],
}
#: The commands a person sends themselves; a part (21) and an assemble (22) are sent by the inscribe of a big file.
SENDABLE = tuple(FORM)


def fields_from_json(name: str, values: list) -> list:
    """A command's fields as a browser sends them in JSON, as `encode` takes them. Raises ValueError."""
    kinds = OPS[CODES[name]][1]
    if not isinstance(values, list) or len(values) != len(kinds):
        raise ValueError(f"{name} takes {len(kinds)} fields")
    out = []
    for kind, value in zip(kinds, values):
        if kind in ("u8", "u32", "var"):
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
                raise ValueError("a number field holds a whole number")
            out.append(int(value))
        elif kind == "str":
            out.append(str(value))
        elif kind == "id":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("a transaction id is 64 hex characters")
            out.append(value)
        elif kind == "ref":
            out.append(_ref_json(value))
        elif kind == "refs":
            if not isinstance(value, list):
                raise ValueError("parts are a list of pointers")
            out.append([_ref_json(v) for v in value])
        elif kind == "data":
            if value is None or isinstance(value, str):
                out.append(value)
            else:
                out.append(_ref_json(value))
        elif kind == "rest":
            if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
                raise ValueError("data is hex")
            out.append(bytes.fromhex(value))
    return out


def _ref_json(value) -> tuple[str, str]:
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)
            and re.fullmatch(r"[0-9a-f]{64}", value[1])):
        raise ValueError("a pointer is [chain, txid]")
    return value[0], value[1]


def send_outputs(payload: bytes, sender_script: bytes, dust: int, target_script: bytes | None = None,
                 target_value: int = 0) -> list[tuple[int, bytes]]:
    """The outputs of a sent command, before its change: the payload's OP_RETURNs first (where `payload` reads it),
    then what pays the target, then -- when the payload takes more than one OP_RETURN -- enough outputs back to the
    sender that at most half of the outputs are data, which Kotia's Core needs to relay it."""
    datas = [(0, script) for script in data_scripts(payload)]
    pays = [(max(target_value, dust), target_script)] if target_script is not None else []
    extra = max(0, len(datas) - len(pays)) if len(datas) > 1 else 0
    return datas + pays + [(dust, sender_script)] * extra


# --- which transaction is a command ------------------------------------------------------------------------------

def _pushes(script: bytes) -> tuple[bytes, bool] | None:
    """(the data pushed, whether every opcode was a data push) after OP_RETURN; None when it is not Core's
    TX_NULL_DATA (OP_RETURN, then push opcodes only, each whole)."""
    if script[:1] != b"\x6a":
        return None
    data, clean, at = bytearray(), True, 1
    while at < len(script):
        op = script[at]
        at += 1
        if op > 0x60:
            return None
        if op <= 0x4E:
            if op < 0x4C:
                size = op
            else:
                width = {0x4C: 1, 0x4D: 2, 0x4E: 4}[op]
                if at + width > len(script):
                    return None
                size = int.from_bytes(script[at:at + width], "little")
                at += width
            if at + size > len(script):
                return None
            data += script[at:at + size]
            at += size
        else:
            clean = False                                       # OP_1NEGATE, OP_RESERVED, OP_1 .. OP_16
    return bytes(data), clean


def payload(tx: rawchain.Tx) -> bytes | None:
    """The MoonBrix 2 payload of `tx`, or None when it is no MoonBrix 2 transaction. Raises Rejected when it is one
    but one of its data outputs holds something other than data pushes."""
    found = [got for got in (_pushes(script) for _value, script in tx.vout) if got is not None]
    if not found or not found[0][0].startswith(MAGIC):
        return None
    if not all(clean for _data, clean in found):
        raise Rejected("an OP_RETURN output holds something other than data pushes")
    return b"".join(data for data, _clean in found)


def address_of(script: bytes, params) -> str | None:
    """The address a script pays as Kotia's Core prints it (a key's address for a pay-to-pubkey coin too)."""
    found = rawchain.script_pubkey(script, params).get("addresses") or []
    return found[0] if found else None


# --- names -----------------------------------------------------------------------------------------------------

#: Names that address something (TLDs, space names, folders and files) are lowercase only, so `rpg.lwq/www/index.html`
#: means one thing however it is typed.
TLD_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,14}[a-z0-9])?")
SPACE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
FILE_NAME = re.compile(r"[a-z0-9._-]{1,64}")
TICKER = re.compile(r"[A-Z]{3,8}")              # letters only: no G0LD beside GOLD (tbuuol)
TYPE = re.compile(r"[ -~]{3,100}")


def _control(ch: str) -> bool:
    return ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F


def text(raw: bytes, most: int, least: int = 0, what: str = "a name", lines: bool = False) -> str:
    if not least <= len(raw) <= most:
        raise Rejected(f"{what} of {len(raw)} bytes ({least} to {most})")
    try:
        got = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"{what} that is not UTF-8") from None
    if any(_control(ch) and not (lines and ch in "\n\t") for ch in got):
        raise Rejected(f"{what} with a control character")
    return got


def tld_name(raw: bytes) -> str:
    got = raw.decode("ascii", "replace")
    if not TLD_NAME.fullmatch(got):
        raise Rejected("a TLD is 1 to 16 of a-z, 0-9 and -, not starting or ending with -")
    return got


def ticker_name(raw: bytes) -> str:
    got = raw.decode("ascii", "replace")
    if not TICKER.fullmatch(got):
        raise Rejected("a ticker is 3 to 8 of A-Z, letters only")
    return got


def type_name(raw: bytes) -> str:
    got = raw.decode("ascii", "replace")
    if not TYPE.fullmatch(got) or "/" not in got:
        raise Rejected("a type is 3 to 100 bytes of printable ASCII with a /")
    return got


def path_names(raw: bytes) -> list[str]:
    path = raw.decode("ascii", "replace")
    names = path.split("/")
    if not 1 <= len(raw) <= 255 or not all(FILE_NAME.fullmatch(n) and n not in (".", "..") for n in names):
        raise Rejected("a path is 1 to 255 bytes: names of 1 to 64 of a-z, 0-9, ., _ and - joined by /, "
                       "none of them . or ..")
    return names


def space_name(raw: bytes) -> str:
    got = raw.decode("ascii", "replace")
    if got and not SPACE_NAME.fullmatch(got):
        raise Rejected("a space's name is 1 to 32 of a-z, 0-9 and -, not starting or ending with -")
    return got


def ip(space: int) -> str:
    return ".".join(str(space >> shift & 0xFF) for shift in (24, 16, 8, 0))


def space_key(tld: str, space: int) -> str:
    return f"{ip(space)}.{tld}"


# --- the state -----------------------------------------------------------------------------------------------

def empty() -> dict[str, Any]:
    return {"version": VERSION, "tlds": {}, "spaces": {}, "inscriptions": {}, "parts": {}, "properties": {},
            "offers": {}, "reservations": {}, "log": []}


class Book:
    """MoonBrix 2's commands on one document, changed in place. `apply` either applies a command whole or raises
    Rejected and leaves the document as it was."""

    def __init__(self, doc: dict[str, Any]):
        self.doc = doc

    def apply(self, height: int, position: int, txid: str, data: bytes, sender: str | None,
              outputs: list[tuple[str | None, int]], fetch_part: Callable[[str], bytes] | None = None) -> str:
        """Command `data` (a payload) of transaction `txid`, the `position`-th of block `height`. `outputs` are the
        transaction's (address or None, koinu). `fetch_part(txid)` gives a part's data (op 21) for an assemble.
        Returns what it did, for the log."""
        op, values = decode(data)
        name, _fields, wants = OPS[op]
        if sender is None:
            raise Rejected("the coin the first input spends pays no address")
        target = next((a for a, _v in outputs if a is not None and a != sender), None)
        if wants == "need" and target is None:
            raise Rejected("no output pays an address other than the sender's")
        if wants == "maybe" and target is None:
            target = sender
        self.height, self.position, self.txid, self.sender = height, position, txid, sender
        self.target, self.outputs, self.fetch_part = target, outputs, fetch_part
        return getattr(self, "op_" + name.replace(" ", "_"))(*values)

    # TLDs -----------------------------------------------------------------------------------------------------

    def _mode(self, mode: int) -> str:
        if mode not in (WHITELIST, BLACKLIST):
            raise Rejected("a list mode is 00 (whitelist) or 01 (blacklist)")
        return "whitelist" if mode == WHITELIST else "blacklist"

    def _owned_tld(self, raw: bytes) -> tuple[str, dict]:
        tld = tld_name(raw)
        got = self.doc["tlds"].get(tld)
        if got is None:
            raise Rejected(f"no TLD {tld}")
        if got["owner"] != self.sender:
            raise Rejected(f"the sender does not own {tld}")
        return tld, got

    def op_claim_tld(self, raw: bytes, mode: int, message: bytes) -> str:
        tld = tld_name(raw)
        if tld in self.doc["tlds"]:
            raise Rejected(f"{tld} is taken")
        self.doc["tlds"][tld] = {"owner": self.sender, "mode": self._mode(mode), "list": [], "height": self.height,
                                 "txid": self.txid, "message": text(message, 80, what="a message")}
        return f"{tld} claimed"

    def op_give_tld(self, raw: bytes) -> str:
        tld, got = self._owned_tld(raw)
        got["owner"] = self.target
        return f"{tld} given to {self.target}"

    def op_set_list_mode(self, raw: bytes, mode: int) -> str:
        tld, got = self._owned_tld(raw)
        got["mode"] = self._mode(mode)
        return f"{tld} is a {got['mode']}"

    def op_list_add(self, raw: bytes) -> str:
        tld, got = self._owned_tld(raw)
        if self.target in got["list"]:
            raise Rejected(f"{self.target} is on {tld}'s list already")
        got["list"].append(self.target)
        return f"{self.target} put on {tld}'s list"

    def op_list_remove(self, raw: bytes) -> str:
        tld, got = self._owned_tld(raw)
        if self.target not in got["list"]:
            raise Rejected(f"{self.target} is not on {tld}'s list")
        got["list"].remove(self.target)
        return f"{self.target} taken off {tld}'s list"

    def op_list_clear(self, raw: bytes) -> str:
        tld, got = self._owned_tld(raw)
        got["list"] = []
        return f"{tld}'s list cleared"

    # spaces ---------------------------------------------------------------------------------------------------

    def _owned_space(self, raw: bytes, space: int) -> tuple[str, dict]:
        key = space_key(tld_name(raw), space)
        got = self.doc["spaces"].get(key)
        if got is None:
            raise Rejected(f"no space {key}")
        if got["owner"] != self.sender:
            raise Rejected(f"the sender does not own {key}")
        return key, got

    def op_claim_space(self, raw: bytes, space: int) -> str:
        tld = tld_name(raw)
        owner = self.doc["tlds"].get(tld)
        if owner is None:
            raise Rejected(f"no TLD {tld}")
        key = space_key(tld, space)
        if key in self.doc["spaces"]:
            raise Rejected(f"{key} is taken")
        listed = self.sender in owner["list"]
        if owner["owner"] != self.sender and listed != (owner["mode"] == "whitelist"):
            raise Rejected(f"{tld}'s {owner['mode']} does not let the sender claim")
        self.doc["spaces"][key] = {"tld": tld, "space": space, "owner": self.sender, "name": "", "root": {},
                                   "height": self.height, "txid": self.txid}
        return f"{key} claimed"

    def op_give_space(self, raw: bytes, space: int) -> str:
        key, got = self._owned_space(raw, space)
        got["owner"] = self.target
        return f"{key} given to {self.target}"

    def op_name_space(self, raw: bytes, space: int, name: bytes) -> str:
        key, got = self._owned_space(raw, space)
        named = space_name(name)
        others = (s for k, s in self.doc["spaces"].items() if s["tld"] == got["tld"] and k != key)
        if named and any(s["name"] == named for s in others):
            raise Rejected(f"{named}.{got['tld']} is taken")
        got["name"] = named
        return f"{key} is {named}.{got['tld']}" if named else f"{key}'s name cleared"

    # inscriptions ---------------------------------------------------------------------------------------------

    def _inscription(self, kind: bytes, name: bytes, size: int, sha: str, parts: list[str] | None) -> str:
        self.doc["inscriptions"][self.txid] = {
            "type": type_name(kind), "name": text(name, 64), "size": size, "sha256": sha, "by": self.sender,
            "height": self.height, "parts": parts}
        return f"{self.doc['inscriptions'][self.txid]['type']}, {size} bytes"

    def op_inscribe(self, kind: bytes, name: bytes, data: bytes) -> str:
        return self._inscription(kind, name, len(data), hashlib.sha256(data).hexdigest(), None)

    def op_part(self, data: bytes) -> str:
        self.doc["parts"][self.txid] = [self.height, self.position, len(data)]
        return f"{len(data)} bytes"

    def op_assemble(self, kind: bytes, name: bytes, size: int, sha: str, refs: list[tuple[bytes, str]]) -> str:
        type_name(kind), text(name, 64)
        parts = []
        for ref in refs:
            got = self._ref(ref, known=False)
            if got["chain"] != HOME_CHAIN:
                raise Rejected(f"a part on {got['chain']}: parts on another chain are not read here")
            parts.append(got["txid"])
        total = 0
        for part in parts:
            got = self.doc["parts"].get(part)
            if got is None or (got[0], got[1]) >= (self.height, self.position):
                raise Rejected(f"{part} is no part confirmed before this command")
            total += got[2]
        if total != size:
            raise Rejected(f"the parts are {total} bytes, not {size}")
        if self.fetch_part is None:
            raise MoonBrixError("no way to fetch the parts' data")
        digest = hashlib.sha256()
        for part in parts:
            digest.update(self.fetch_part(part))
        if digest.hexdigest() != sha:
            raise Rejected("the parts' SHA-256 is not the one named")
        return self._inscription(kind, name, size, sha, parts)

    # folders and files ----------------------------------------------------------------------------------------

    @staticmethod
    def _folder(root: dict, names: list[str]) -> dict | None:
        """The children of the folder at `names` (root for []), or None when no folder is there."""
        here = root
        for name in names:
            node = here.get(name)
            if node is None or "dir" not in node:
                return None
            here = node["dir"]
        return here

    def _spot(self, raw: bytes, space: int, path: bytes) -> tuple[str, dict, str]:
        """(space key, the parent folder's children, the last name) of a path whose parent folder exists."""
        key, got = self._owned_space(raw, space)
        names = path_names(path)
        parent = self._folder(got["root"], names[:-1])
        if parent is None:
            raise Rejected(f"no folder {'/'.join(names[:-1])} in {key}")
        return key, parent, names[-1]

    def _ref(self, ref: tuple[bytes, str], known: bool = True) -> dict:
        """A pointer as kept: {chain, txid, checked}. One to this chain must name an inscription it has (with
        `known`); one to another chain is kept as it is, unchecked, until a reader that reads both can check it."""
        chain_raw, txid = ref
        chain = chain_raw.decode("ascii", "replace") or HOME_CHAIN
        if not CHAIN.fullmatch(chain):
            raise Rejected("a chain in a pointer is 2 to 8 of a-z and 0-9 (its ticker), or empty for this chain")
        home = chain == HOME_CHAIN
        if home and known and txid not in self.doc["inscriptions"]:
            raise Rejected(f"no inscription {txid}")
        return {"chain": chain, "txid": txid, "checked": home}

    def _data(self, got: tuple[str, Any] | None) -> dict | None:
        """A data field as kept: None, {text}, or {ref: a pointer}."""
        if got is None:
            return None
        if got[0] == "text":
            return {"text": text(got[1], MAX_DATA_TEXT, what="data", lines=True)}
        return {"ref": self._ref(got[1])}

    def op_make_folder(self, raw: bytes, space: int, path: bytes) -> str:
        key, parent, name = self._spot(raw, space, path)
        if name in parent:
            raise Rejected(f"something is at {path.decode()} already")
        parent[name] = {"dir": {}}
        return f"folder {path.decode()} in {key}"

    def op_put_file(self, raw: bytes, space: int, path: bytes, ref: tuple[bytes, str]) -> str:
        key, parent, name = self._spot(raw, space, path)
        if name in parent:
            raise Rejected(f"something is at {path.decode()} already")
        parent[name] = {"file": self._ref(ref)}
        return f"file {path.decode()} in {key}"

    def op_replace_file(self, raw: bytes, space: int, path: bytes, ref: tuple[bytes, str]) -> str:
        key, parent, name = self._spot(raw, space, path)
        if "file" not in parent.get(name, {}):
            raise Rejected(f"no file at {path.decode()}")
        parent[name] = {"file": self._ref(ref)}
        return f"file {path.decode()} in {key} replaced"

    def op_remove(self, raw: bytes, space: int, path: bytes) -> str:
        key, parent, name = self._spot(raw, space, path)
        node = parent.get(name)
        if node is None:
            raise Rejected(f"nothing at {path.decode()}")
        if node.get("dir"):
            raise Rejected(f"the folder {path.decode()} is not empty")
        del parent[name]
        return f"{path.decode()} removed from {key}"

    def op_move(self, raw: bytes, space: int, source: bytes, to: bytes) -> str:
        key, got = self._owned_space(raw, space)
        old, new = path_names(source), path_names(to)
        old_parent = self._folder(got["root"], old[:-1])
        if old_parent is None or old[-1] not in old_parent:
            raise Rejected(f"nothing at {source.decode()}")
        new_parent = self._folder(got["root"], new[:-1])
        if new_parent is None:
            raise Rejected(f"no folder {'/'.join(new[:-1])} in {key}")
        if new[-1] in new_parent:
            raise Rejected(f"something is at {to.decode()} already")
        if new[:len(old)] == old:
            raise Rejected("a folder cannot go inside itself")
        new_parent[new[-1]] = old_parent.pop(old[-1])
        return f"{source.decode()} moved to {to.decode()} in {key}"

    def op_join(self, raw: bytes, space: int, source: bytes, into: bytes) -> str:
        key, got = self._owned_space(raw, space)
        old, new = path_names(source), path_names(into)
        old_parent = self._folder(got["root"], old[:-1])
        moving = self._folder(got["root"], old)
        target = self._folder(got["root"], new)
        if moving is None:
            raise Rejected(f"no folder {source.decode()} in {key}")
        if target is None:
            raise Rejected(f"no folder {into.decode()} in {key}")
        if new[:len(old)] == old:
            raise Rejected("a folder cannot be joined into itself or a folder inside it")
        clash = sorted(set(moving) & set(target))
        if clash:
            raise Rejected(f"{into.decode()} has {clash[0]} already")
        target.update(moving)
        del old_parent[old[-1]]
        return f"{source.decode()} joined into {into.decode()} in {key}"

    # properties -----------------------------------------------------------------------------------------------

    def _property(self, raw: bytes, owned: bool = False) -> tuple[str, dict]:
        ticker = ticker_name(raw)
        got = self.doc["properties"].get(ticker)
        if got is None:
            raise Rejected(f"no property {ticker}")
        if owned and got["owner"] != self.sender:
            raise Rejected(f"the sender does not own {ticker}")
        return ticker, got

    @staticmethod
    def _add(holders: dict, who: str, amount: int) -> None:
        holders[who] = holders.get(who, 0) + amount

    @staticmethod
    def _take(holders: dict, who: str, amount: int, ticker: str) -> None:
        if amount < 1:
            raise Rejected("an amount is 1 or more")
        if holders.get(who, 0) < amount:
            raise Rejected(f"the sender holds {holders.get(who, 0)} {ticker}, not {amount}")
        holders[who] -= amount
        if not holders[who]:
            del holders[who]

    def op_create_property(self, raw: bytes, space: int, ticker_raw: bytes, name: bytes, kind: int, decimals: int,
                           supply: int, grant: tuple[str, Any] | None) -> str:
        key, _got = self._owned_space(raw, space)
        ticker = ticker_name(ticker_raw)
        if ticker in self.doc["properties"]:
            raise Rejected(f"{ticker} is taken")
        if kind not in KINDS:
            raise Rejected("a kind is 00 (fixed), 01 (managed) or 02 (NFT)")
        if not 0 <= decimals <= 8 or (kind == NFT and decimals):
            raise Rejected("decimals are 0 to 8, and 0 for an NFT")
        if (kind == FIXED) != (supply > 0):
            raise Rejected("a fixed property has a supply of 1 or more, any other a supply of 0")
        prop = {"name": text(name, 32, 1), "kind": KINDS[kind], "decimals": decimals, "owner": self.sender,
                "space": key, "supply": supply, "locked": kind == FIXED, "grant": self._data(grant), "holders": {},
                "items": {}, "minted": 0, "height": self.height, "txid": self.txid}
        if supply:
            prop["holders"][self.sender] = supply
        self.doc["properties"][ticker] = prop
        return f"{ticker}, {KINDS[kind]}" + (f", {supply}" if supply else "")

    def op_give_property(self, raw: bytes) -> str:
        ticker, got = self._property(raw, owned=True)
        got["owner"] = self.target
        return f"{ticker} given to {self.target}"

    def op_lock_property(self, raw: bytes) -> str:
        ticker, got = self._property(raw, owned=True)
        if got["locked"]:
            raise Rejected(f"{ticker} is locked already")
        got["locked"] = True
        return f"{ticker} locked"

    def op_mint(self, raw: bytes, amount: int) -> str:
        ticker, got = self._property(raw, owned=True)
        if got["kind"] != "managed":
            raise Rejected(f"{ticker} is {got['kind']}: only a managed property is minted")
        if got["locked"]:
            raise Rejected(f"{ticker} is locked")
        if amount < 1 or got["supply"] + amount >= U64:
            raise Rejected("an amount is 1 or more, and the supply stays below 2^64")
        got["supply"] += amount
        self._add(got["holders"], self.target, amount)
        return f"{amount} {ticker} to {self.target}"

    def _fungible(self, raw: bytes) -> tuple[str, dict]:
        ticker, got = self._property(raw)
        if got["kind"] == "nft":
            raise Rejected(f"{ticker} is an NFT")
        return ticker, got

    def op_send(self, raw: bytes, amount: int) -> str:
        ticker, got = self._fungible(raw)
        self._take(got["holders"], self.sender, amount, ticker)
        self._add(got["holders"], self.target, amount)
        return f"{amount} {ticker} to {self.target}"

    def op_burn(self, raw: bytes, amount: int) -> str:
        ticker, got = self._fungible(raw)
        self._take(got["holders"], self.sender, amount, ticker)
        got["supply"] -= amount
        return f"{amount} {ticker} burnt"

    def op_mint_nft(self, raw: bytes, grant: tuple[str, Any] | None) -> str:
        ticker, got = self._property(raw, owned=True)
        if got["kind"] != "nft":
            raise Rejected(f"{ticker} is no NFT")
        if got["locked"]:
            raise Rejected(f"{ticker} is locked")
        kept = self._data(grant)
        got["minted"] += 1
        got["supply"] += 1
        got["items"][str(got["minted"])] = {"holder": self.target, "grant": kept, "issuer": None,
                                            "holder_data": None}
        return f"{ticker} #{got['minted']} to {self.target}"

    def _item(self, raw: bytes, item: int) -> tuple[str, dict]:
        ticker, got = self._property(raw)
        if got["kind"] != "nft":
            raise Rejected(f"{ticker} is no NFT")
        thing = got["items"].get(str(item))
        if thing is None or thing["holder"] != self.sender:
            raise Rejected(f"the sender does not hold {ticker} #{item}")
        return ticker, thing

    def op_send_nft(self, raw: bytes, item: int) -> str:
        ticker, thing = self._item(raw, item)
        thing["holder"] = self.target
        return f"{ticker} #{item} to {self.target}"

    def op_set_issuer_data(self, raw: bytes, item: int, data: tuple[str, Any] | None) -> str:
        ticker, got = self._property(raw, owned=True)
        if got["kind"] != "nft":
            raise Rejected(f"{ticker} is no NFT")
        thing = got["items"].get(str(item))
        if thing is None:
            raise Rejected(f"no {ticker} #{item}")
        thing["issuer"] = self._data(data)
        return f"{ticker} #{item}: issuer data set"

    def op_set_holder_data(self, raw: bytes, item: int, data: tuple[str, Any] | None) -> str:
        ticker, thing = self._item(raw, item)
        thing["holder_data"] = self._data(data)
        return f"{ticker} #{item}: holder data set"

    # DEX ------------------------------------------------------------------------------------------------------
    # As on Omni's DEX: a buyer first reserves (52), and the reserved tokens are theirs to pay for (53) until the
    # offer's window closes; the seller can no longer take them back. A deposit, part of the price, paid to the
    # seller with the reservation, makes reserving without paying cost something.

    def _paid_to(self, address: str) -> int:
        return sum(value for a, value in self.outputs if a == address)

    def _offer(self, offer_id: str) -> dict:
        offer = self.doc["offers"].get(offer_id)
        if offer is None:
            raise Rejected(f"no offer {offer_id}")
        return offer

    def _release(self, offer_id: str, offer: dict, amount: int, to: str) -> None:
        """`amount` of an offer's tokens (an NFT: its item) out of the offer, to `to`."""
        got = self.doc["properties"][offer["ticker"]]
        if offer["item"] is not None:
            got["items"][str(offer["item"])]["holder"] = to
        else:
            self._add(got["holders"], to, amount)

    def _tidy(self, offer_id: str) -> None:
        offer = self.doc["offers"][offer_id]
        if not offer["left"] and not any(r["offer"] == offer_id for r in self.doc["reservations"].values()):
            del self.doc["offers"][offer_id]

    def expire(self, height: int) -> list[str]:
        """Reservations whose window closed before `height`: their tokens go back to the offer (or, when it was
        cancelled, to the seller). Done at the start of every block read, before its commands."""
        gone = []
        for rid, r in sorted(self.doc["reservations"].items()):
            if r["expires"] < height:
                offer = self.doc["offers"][r["offer"]]
                if offer["open"]:
                    offer["left"] += r["amount"]
                else:
                    self._release(r["offer"], offer, r["amount"], offer["seller"])
                del self.doc["reservations"][rid]
                self._tidy(r["offer"])
                gone.append(rid)
        return gone

    def op_offer(self, raw: bytes, amount: int, price: int, window: int, deposit: int) -> str:
        if price < 1:
            raise Rejected("a price is 1 koinu or more")
        if not 1 <= window <= MAX_WINDOW:
            raise Rejected(f"a payment window is 1 to {MAX_WINDOW} blocks")
        if deposit > price:
            raise Rejected("a deposit is at most the price")
        ticker, got = self._property(raw)
        if got["kind"] == "nft":
            _t, thing = self._item(raw, amount)
            thing["holder"] = None
            item, units, what = amount, 1, f"{ticker} #{amount}"
        else:
            self._take(got["holders"], self.sender, amount, ticker)
            item, units, what = None, amount, f"{amount} {ticker}"
        self.doc["offers"][self.txid] = {"ticker": ticker, "item": item, "amount": units, "left": units,
                                         "price": price, "window": window, "deposit": deposit,
                                         "seller": self.sender, "open": True, "height": self.height}
        return f"{what} for {price} koinu, {window} blocks to pay"

    def op_cancel(self, offer_id: str) -> str:
        offer = self._offer(offer_id)
        if offer["seller"] != self.sender:
            raise Rejected("the sender did not make this offer")
        if not offer["open"]:
            raise Rejected("the offer is cancelled already")
        if offer["left"]:
            self._release(offer_id, offer, offer["left"], self.sender)
        offer["open"], offer["left"] = False, 0
        self._tidy(offer_id)
        return f"offer {offer_id} cancelled; what is reserved stays reserved"

    def op_reserve(self, offer_id: str, amount: int) -> str:
        offer = self._offer(offer_id)
        if not offer["open"]:
            raise Rejected("the offer is cancelled")
        if offer["seller"] == self.sender:
            raise Rejected("the seller cannot reserve their own offer")
        if not 1 <= amount <= offer["left"]:
            raise Rejected(f"{offer['left']} of the offer is left, not {amount}")
        due = -(-offer["price"] * amount // offer["amount"])                # rounded up
        deposit = -(-offer["deposit"] * amount // offer["amount"])
        paid = self._paid_to(offer["seller"])
        if paid < deposit:
            raise Rejected(f"the seller is paid {paid} koinu, not the deposit of {deposit}")
        offer["left"] -= amount
        if paid >= due:                                                     # paid in full at once
            self._release(offer_id, offer, amount, self.sender)
            self._tidy(offer_id)
            return f"{amount} of offer {offer_id} bought for {paid} koinu"
        self.doc["reservations"][self.txid] = {"offer": offer_id, "buyer": self.sender, "amount": amount,
                                               "due": due, "paid": paid, "expires": self.height + offer["window"],
                                               "height": self.height}
        return f"{amount} of offer {offer_id} reserved until block {self.height + offer['window']}"

    def op_pay(self, reservation_id: str) -> str:
        r = self.doc["reservations"].get(reservation_id)
        if r is None:
            raise Rejected(f"no reservation {reservation_id} (paid, or its window closed)")
        if r["buyer"] != self.sender:
            raise Rejected("the sender did not make this reservation")
        offer = self.doc["offers"][r["offer"]]
        paid = self._paid_to(offer["seller"])
        if r["paid"] + paid < r["due"]:
            raise Rejected(f"the seller is paid {paid} koinu, {r['due'] - r['paid']} are due")
        self._release(r["offer"], offer, r["amount"], self.sender)
        del self.doc["reservations"][reservation_id]
        self._tidy(r["offer"])
        return f"{r['amount']} of offer {r['offer']} paid for"


# --- following the chain -----------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS undo (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, before TEXT NOT NULL);
"""


class MoonBrix:
    """MoonBrix 2's state on one chain, in one SQLite file, followed block by block."""

    def __init__(self, path: str | Path, params) -> None:
        self.path = Path(path)
        self.params = params
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.execute("DROP TABLE IF EXISTS skipped")           # the prototype's reader kept one
        self.lock = threading.RLock()
        if self._meta("doc") and json.loads(self._meta("doc")).get("version") != VERSION:
            self.reset()                                            # a document of the prototype: read again

    def close(self) -> None:
        self.conn.close()

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

    def document(self) -> dict[str, Any]:
        """The state (a copy)."""
        with self.lock:
            got = self._meta("doc")
        return json.loads(got) if got else empty()

    def reset(self) -> None:
        with self.lock, self.conn:
            for table in ("meta", "undo"):
                self.conn.execute(f"DELETE FROM {table}")

    def _set(self, height: int, best: str, doc: dict | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('height', ?)", (str(height),))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('best', ?)", (best,))
        if doc is not None:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('doc', ?)", (json.dumps(doc),))

    def connect(self, height: int, block_hash: str, txs: Iterable[rawchain.Tx],
                spent_output: Callable[[str, int], tuple[bytes, int]],
                fetch_tx: Callable[[str], rawchain.Tx] | None = None) -> bool:
        """Read block `height`'s transactions, in block order (only those carrying an OP_RETURN are needed).
        Returns whether the document changed. `spent_output(txid, vout)` -> (script, value) of a coin made before;
        `fetch_tx(txid)` a transaction of before, for an assemble's parts (the Core). Raises MoonBrixError, and
        keeps nothing of the block, when the node cannot give what a command needs."""
        with self.lock:
            if self.height is not None and height <= self.height:
                raise MoonBrixError(f"block {height} is not past the document's tip {self.height}")
            doc = self.document()
            before = json.dumps(doc)
            if height >= ACTIVATION:
                for rid in Book(doc).expire(height):
                    doc["log"].append({"height": height, "txid": rid, "op": "expiry", "ok": True,
                                       "what": "reservation's window closed; its tokens went back"})
                for position, tx in enumerate(txs):
                    self._read(doc, height, position, tx, spent_output, fetch_tx)
            changed = json.dumps(doc) != before
            with self.conn:
                if changed:
                    self.conn.execute("INSERT OR REPLACE INTO undo VALUES (?,?,?)", (height, block_hash, before))
                    self.conn.execute("DELETE FROM undo WHERE height<=?", (height - UNDO_DEPTH,))
                self._set(height, block_hash, doc if changed else None)
            return changed

    def _read(self, doc, height, position, tx, spent_output, fetch_tx) -> None:
        entry = {"height": height, "txid": tx.txid}
        try:
            data = payload(tx)
            if data is None:
                return
            entry["op"] = OPS[data[len(MAGIC)]][0] if len(data) > len(MAGIC) and data[len(MAGIC)] in OPS else "?"
            first = tx.vin[0] if tx.vin else None
            sender = None
            if first and first.get("txid"):
                sender = address_of(spent_output(first["txid"], first["vout"])[0], self.params)
            entry["by"] = sender
            outputs = [(address_of(script, self.params), value) for value, script in tx.vout]

            def fetch_part(txid: str) -> bytes:
                if fetch_tx is None:
                    raise MoonBrixError("no way to fetch a part")
                got = payload(fetch_tx(txid))
                op, values = decode(got) if got else (None, None)
                if op != CODES["part"]:
                    raise MoonBrixError(f"part {txid} reads as no part now")
                return values[0]

            work = json.loads(json.dumps(doc))
            entry["what"] = Book(work).apply(height, position, tx.txid, data, sender, outputs, fetch_part)
            doc.clear()
            doc.update(work)
            entry["ok"] = True
        except Rejected as exc:
            entry["ok"], entry["why"] = False, str(exc)
        doc["log"].append(entry)

    def roll_back(self, to: int, best: str) -> None:
        """Back to the document after block `to` (a reorg): whatever blocks above it changed is undone."""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT before FROM undo WHERE height>? ORDER BY height LIMIT 1", (to,)).fetchone()
            self.conn.execute("DELETE FROM undo WHERE height>?", (to,))
            if row is not None:
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('doc', ?)", (row[0],))
            self._set(to, best, None)

    def undo_heights(self) -> list[tuple[int, str]]:
        return list(self.conn.execute("SELECT height, hash FROM undo ORDER BY height DESC"))


def fetch_tx_by_rpc(rpc, params) -> Callable[[str], rawchain.Tx]:
    def fetch(txid: str) -> rawchain.Tx:
        return rawchain.read_tx(bytes.fromhex(rpc.call("getrawtransaction", txid, 0)), txid, timed=params.tx_time)
    return fetch


def follow(mb: MoonBrix, uset, rpc, params) -> int:
    """Bring the document to the unspent set's tip, reading only the blocks the set says carry data. A tip that left
    the chain (a reorg) is rolled back first: to the newest block it changed that is still on the chain, or read again
    from the start when none is (cheap: data blocks are few). Returns how many data blocks were read."""
    from .utxoset import spent_output_by_rpc
    tip, tip_hash = uset.height, uset.best
    if tip is None or tip_hash is None:
        return 0
    if mb.height is not None:
        if mb.height > tip or str(rpc.call("getblockhash", mb.height)) != mb.best:
            for height, block_hash in mb.undo_heights():
                if height <= tip and str(rpc.call("getblockhash", height)) == block_hash:
                    mb.roll_back(height, block_hash)
                    break
            else:
                mb.reset()
    since = ACTIVATION if mb.height is None else max(mb.height + 1, ACTIVATION)
    look, fetch = spent_output_by_rpc(rpc), fetch_tx_by_rpc(rpc, params)
    read = 0
    for height, txids in uset.data_blocks(since):
        if height > tip:
            break
        raws = rpc.batch([("getrawtransaction", [t, 0]) for t in txids])
        txs = [rawchain.read_tx(bytes.fromhex(raw), t, timed=params.tx_time) for t, raw in zip(txids, raws)]
        mb.connect(height, str(rpc.call("getblockhash", height)), txs, look, fetch)
        read += 1
    if mb.height != tip:
        with mb.lock, mb.conn:
            mb._set(tip, tip_hash, None)
    return read


# --- reading it -------------------------------------------------------------------------------------------------

def content(doc: dict[str, Any], fetch_tx: Callable[[str], rawchain.Tx], txid: str) -> tuple[str, bytes]:
    """An inscription's type and its bytes, from the chain, checked against the SHA-256 it was read with."""
    ins = doc["inscriptions"].get(txid)
    if ins is None:
        raise KeyError(f"no MoonBrix inscription {txid}")
    pieces = []
    for part in ins["parts"] or [txid]:
        got = payload(fetch_tx(part))
        op, values = decode(got) if got else (None, None)
        if op == CODES["inscribe"] and ins["parts"] is None:
            pieces.append(values[2])
        elif op == CODES["part"] and ins["parts"] is not None:
            pieces.append(values[0])
        else:
            raise KeyError(f"{part} does not carry this inscription's data")
    data = b"".join(pieces)
    if hashlib.sha256(data).hexdigest() != ins["sha256"]:
        raise KeyError(f"{txid}: the data on the chain is not what was read")
    return ins["type"], data


def _space_of(doc: dict[str, Any], tld: str, labels: list[str]) -> str | None:
    """The key of the space `labels` names in `tld`: its name (`rpg`) or its number (`19.55.66.132`)."""
    if len(labels) == 1:
        return next((k for k, sp in doc["spaces"].items() if sp["tld"] == tld and sp["name"] == labels[0]), None)
    if len(labels) == 4 and all(x.isdigit() and int(x) <= 255 for x in labels):
        key = f"{'.'.join(str(int(x)) for x in labels)}.{tld}"
        return key if key in doc["spaces"] else None
    return None


def find(doc: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """What a search names, for the explorer: every match, most specific first.
    - `lwq`: the TLD and all its spaces; `rpg.lwq` or `19.55.66.132.lwq`: that space, its files and properties;
      `rpg.lwq/www/index.html`: that folder or file. Names are lowercase, so the search is too.
    - a ticker (`MBX`, any case), a transaction id (an inscription, offer or reservation), or an address."""
    q = query.strip()
    found: list[dict[str, Any]] = []
    if not q:
        return found
    if re.fullmatch(r"[0-9a-fA-F]{64}", q):
        txid = q.lower()
        for kind, table in (("inscription", "inscriptions"), ("offer", "offers"), ("reservation", "reservations")):
            if txid in doc[table]:
                found.append({"kind": kind, "id": txid, "it": doc[table][txid]})
        return found
    lower = q.lower()
    name, slash, path = lower.partition("/")
    labels = name.split(".")
    tld = labels[-1]
    if tld in doc["tlds"]:
        if len(labels) == 1 and not slash:
            spaces = sorted((k, sp) for k, sp in doc["spaces"].items() if sp["tld"] == tld)
            found.append({"kind": "tld", "id": tld, "it": doc["tlds"][tld], "spaces": spaces})
        else:
            key = _space_of(doc, tld, labels[:-1])
            if key is not None:
                sp = doc["spaces"][key]
                address = f"{sp['name']}.{tld}" if sp["name"] else key
                props = sorted((t, pr) for t, pr in doc["properties"].items() if pr["space"] == key)
                if path.strip("/"):
                    names = path.strip("/").split("/")
                    parent = Book._folder(sp["root"], names[:-1])
                    node = parent.get(names[-1]) if parent is not None else None
                    if node is not None:
                        found.append({"kind": "path", "id": f"{address}/{'/'.join(names)}", "space": key,
                                      "it": node})
                else:
                    found.append({"kind": "space", "id": address, "key": key, "it": sp, "properties": props})
    ticker = q.upper()
    if not slash and "." not in q and ticker in doc["properties"]:
        found.append({"kind": "property", "id": ticker, "it": doc["properties"][ticker]})
    if not found and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{25,35}", q):
        owns = {"tlds": sorted(t for t, v in doc["tlds"].items() if v["owner"] == q),
                "spaces": sorted(k for k, v in doc["spaces"].items() if v["owner"] == q),
                "properties": sorted(t for t, v in doc["properties"].items() if v["owner"] == q)}
        found.append({"kind": "address", "id": q, "held": balances(doc, q), "owns": owns})
    return found


def units(amount: int, decimals: int) -> str:
    """An amount in the smallest unit, written in whole tokens."""
    if not decimals:
        return str(amount)
    whole, part = divmod(amount, 10 ** decimals)
    return f"{whole}.{part:0{decimals}d}".rstrip("0").rstrip(".")


def balances(doc: dict[str, Any], address: str) -> list[dict[str, Any]]:
    """What `address` holds of each property: an amount, or the NFT items. Offers it made are not counted."""
    held = []
    for ticker, p in sorted(doc["properties"].items()):
        if p["kind"] == "nft":
            items = sorted(int(i) for i, thing in p["items"].items() if thing["holder"] == address)
            if items:
                held.append({"ticker": ticker, "name": p["name"], "kind": p["kind"], "items": items})
        elif address in p["holders"]:
            held.append({"ticker": ticker, "name": p["name"], "kind": p["kind"], "amount": p["holders"][address],
                         "decimals": p["decimals"]})
    return held


def main(argv: list[str] | None = None) -> int:
    """python -m arcade.moonbrix follow|status|dump --db FILE --utxoset FILE [--conf F | --datadir D]"""
    import argparse

    from .config import NETWORKS, load_rpc_credentials
    from .rpc import RpcClient
    from .utxoset import UtxoSet

    ap = argparse.ArgumentParser(prog="python -m arcade.moonbrix")
    ap.add_argument("action", choices=("follow", "status", "dump"))
    ap.add_argument("--net", default="kot")
    ap.add_argument("--db", required=True)
    ap.add_argument("--utxoset")
    ap.add_argument("--conf", type=Path)
    ap.add_argument("--datadir", type=Path)
    args = ap.parse_args(argv)
    params = NETWORKS[args.net]
    if not getattr(params, "moonbrix", False):
        raise SystemExit(f"{params.label}: MoonBrix is read on Kotia only (Params.moonbrix)")
    mb = MoonBrix(args.db, params)
    try:
        if args.action == "follow":
            if not args.utxoset:
                raise SystemExit("--utxoset is needed to follow")
            uset = UtxoSet(args.utxoset, params)
            rpc = RpcClient(load_rpc_credentials(params, conf_path=args.conf, datadir=args.datadir))
            print("data blocks read:", follow(mb, uset, rpc, params))
            uset.close()
        doc = mb.document()
        if args.action == "dump":
            print(json.dumps(doc, indent=4))
        else:
            print(json.dumps({"height": mb.height, "best": mb.best,
                              **{k: len(v) for k, v in doc.items() if isinstance(v, (dict, list))}}, indent=1))
    finally:
        mb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
