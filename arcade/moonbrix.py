"""MoonBrix on Kotia, read the way its own Kurator reads it (tbuuol 2026-11-10). KOTIA ONLY (Params.moonbrix).

MoonBrix (github.com/tbuuol/MoonBrix) is tbuuol's OP_RETURN protocol on Kotia: TLD and IP spaces, white- and
blacklists, folders and files, inscriptions, properties and their tokens. Its reference is the C++ program's
`Kurator.cpp`, and the arcade must come to the same state as that program does, byte for byte, or the two show
different owners and balances. So this is a copy of what it does, quirks included (QUIRKS below lists them, so
tbuuol can decide which to change -- in both at once, from a block on). Nothing here is the arcade's own idea.

Which transaction is a command, as MoonBrix's MasterDB, Indexer and Sync pick them:
- Its `vout[0]` is a data output by Core's rule (OP_RETURN followed by pushes only) and Core's disassembly of it
  starts "OP_RETURN " with at least 30 characters after that.
- **Only the first such transaction of a block counts** (the Indexer keeps one per height). A second one in the same
  block is read by nobody.
- It is a command when that disassembly is exactly "OP_RETURN <hex>" with at least 30 hex characters (15 bytes): the
  header. Bytes 0-8 the TLD, 9-12 the IP, 13 the action, 14 the action table (00), 15 the sub-action, 16 on data.
- Its sender (`origin`) is the address of the coin its first input spends.
- Before the protocol's activation (the 30-zero header, Kotia block 1,236,675) nothing else counts.

The state is MoonBrix's five files, the same shapes and key names (`LIBARY/Kotia/*.json`): `lib` (the event log),
`tld`, `fold`, `ins` and `prop`. It is kept as one JSON document in `<net>-moonbrix.sqlite`, with the document as it
was before each block that changed it, so a reorg is rolled back. MoonBrix commands are rare (50 blocks carry data
on Kotia since the activation), so the arcade's unspent set (utxoset.py `data_blocks`) says which blocks to read and
everything else is skipped.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from . import rawchain

#: The activation header, and the TLDs and IPs no command may name (Kurator.hpp ActionCode).
ACTIVATION = "0" * 30
FIRST_TLD, LAST_TLD = "0" * 18, "f" * 18
FIRST_IP, LAST_IP = "0" * 8, "f" * 8
#: Byte 13, the action (Kurator.hpp ActionCode). 0a-13 are named there and read by nothing yet.
CLAIM, OWNER, LISTTYPE, CHANGELIST, NAME, INSCRIBE, FOLDER, FILE, PROPERTY, TOKEN = (
    "00", "01", "02", "03", "04", "05", "06", "07", "08", "09")
#: A meta inscription's chain code (byte 16).
CHAINS = {"00": "Kotia", "01": "Fairbrix"}
#: Blocks whose document before them is kept, so a reorg up to this deep is rolled back rather than read again.
UNDO_DEPTH = 200
U64 = 1 << 64

#: Where the arcade does what Kurator.cpp does and that may not be what was meant, for tbuuol to decide on. Changing
#: one changes the protocol: MoonBrix and the arcade together, from a block on.
QUIRKS = (
    "Only the first command of a block counts; a second transaction in the same block is never read (Indexer.cpp).",
    "A transaction MoonBrix cannot read (an input or output without an address where one is needed, a meta "
    "inscription that is not JSON, a folder or file put where a file already is) stops MoonBrix's sync for good; "
    "the arcade passes over it and goes on.",
    "Folder and file 'already taken' checks never find anything, so a create overwrites a folder or file of the "
    "same name (to_string(...) == name compares a quoted or numbered string with a letters-only name).",
    "A folder created somewhere other than root goes beside 'root' in the IP's entry, not inside it; a file put in "
    "a folder goes inside root (Kurator::folder writes e[location], Kurator::file e['root'][location]).",
    "Deleting a file finds only files at root, and stops early at a folder named like the location "
    "(Kurator::file sub-action 01).",
    "Names, tickers, TLD texts and messages keep only the letters A-Z and a-z (Helper::hexToText); digits and "
    "spaces are dropped.",
    "Anybody may create a property under any TLD.IP, not only its owner (Kurator::property).",
    "A property of fixed supply can still be minted by its owner (Kurator::token sub-action 01).",
    "A mint is logged as 'move' (Kurator::token).",
    "Supplies and balances wrap at 2^64 (unsigned 64-bit sums).",
    "A fixed-supply create cut short (under 51 bytes) logs 'Property create' and creates nothing.",
    "A meta inscription counts only when its JSON says \"next\": false; null or a txid stops MoonBrix's sync "
    "(`!d[\"next\"]` is a conversion to bool).",
)


class MoonBrixError(RuntimeError):
    """The document and the chain disagree (a block that does not follow)."""


class Unreadable(ValueError):
    """A transaction MoonBrix itself would stop at (it throws and its sync never gets past it); skipped here."""


# --- Helper.cpp -------------------------------------------------------------------------------------------------

def text(hexs: str, space: bool = False, full: bool = False) -> str:
    """Helper::hexToText: the letters A-Z and a-z of the bytes (and spaces with `space`); every byte with `full`."""
    raw = bytes.fromhex(hexs[:len(hexs) // 2 * 2])
    if full:
        return raw.decode("latin-1")
    return "".join(chr(b) for b in raw if 65 <= b <= 90 or 97 <= b <= 122 or (space and b == 32))


def ip(hexs: str) -> str:
    """Helper::hexToIP: four bytes as a dotted IPv4."""
    value = int(hexs, 16)
    return ".".join(str(value >> shift & 0xFF) for shift in (24, 16, 8, 0))


def number(hexs: str) -> int:
    """Helper::hexToNumber: big-endian hex, at most 64 bits (stoull)."""
    value = int(hexs, 16)
    if value >= U64:
        raise Unreadable("a number past 64 bits")
    return value


# --- which transaction is a command ------------------------------------------------------------------------------

def _push_only(script: bytes) -> bool:
    """CScript::IsPushOnly: every opcode a push or OP_1NEGATE/OP_RESERVED/OP_1..OP_16, and every push whole."""
    at = 0
    while at < len(script):
        op = script[at]
        at += 1
        if op > 0x60:
            return False
        if 0 < op <= 0x4E:
            if op < 0x4C:
                size = op
            else:
                width = {0x4C: 1, 0x4D: 2, 0x4E: 4}[op]
                if at + width > len(script):
                    return False
                size = int.from_bytes(script[at:at + width], "little")
                at += width
            if at + size > len(script):
                return False
            at += size
    return True


def nulldata(script: bytes) -> bool:
    """Core's TX_NULL_DATA (Solver): OP_RETURN, then pushes only."""
    return script[:1] == b"\x6a" and _push_only(script[1:])


def candidate(tx: rawchain.Tx) -> str | None:
    """The disassembly of `vout[0]` when this transaction takes its block's MoonBrix slot (MasterDB.cpp), else None."""
    if not tx.vout or not nulldata(tx.vout[0][1]):
        return None
    said = rawchain.asm(tx.vout[0][1])
    if not said.startswith("OP_RETURN ") or len(said) - len("OP_RETURN ") < 30:
        return None
    return said


def command(said: str) -> str | None:
    """The header and data (hex) when the slot's disassembly is a command (Sync.cpp BuildLibary), else None."""
    chunks = said.split()
    if len(chunks) == 2 and chunks[0] == "OP_RETURN" and len(chunks[1]) >= 30:
        return chunks[1]
    return None


def address_of(script: bytes, params) -> str | None:
    """`scriptPubKey.addresses[0]` as Kotia's Core prints it (a key's address for a pay-to-pubkey coin too)."""
    found = rawchain.script_pubkey(script, params).get("addresses") or []
    return found[0] if found else None


# --- Kurator.cpp ---------------------------------------------------------------------------------------------------

def empty() -> dict[str, list]:
    return {"lib": [], "tld": [], "fold": [], "ins": [], "prop": []}


class Kurator:
    """Kurator.cpp's actions on one document (`lib`, `tld`, `fold`, `ins`, `prop`), changed in place."""

    def __init__(self, doc: dict[str, list]):
        self.doc = doc
        self.LIB, self.TLD, self.FOLD = doc["lib"], doc["tld"], doc["fold"]
        self.INS, self.PROP = doc["ins"], doc["prop"]

    def apply(self, block: int, txid: str, chunk: str, origin: Callable[[], str | None],
              output: Callable[[int], str | None], outputs: int) -> None:
        """One command. `origin()` is the sender, `output(i)` the address `vout[i]` pays, `outputs` how many there
        are. Raises Unreadable where MoonBrix's own sync would stop."""
        if not self.LIB:
            if chunk[:30] == ACTIVATION:
                self.LIB.append({"Block": block, "TXID": txid, "Event": "MoonBrix GENESIS",
                                 "Action": "Protocol activated", "Message": text(chunk[30:], space=True)})
            return
        owner = origin()
        if owner is None:
            raise Unreadable("the coin the first input spends pays no address")
        act = chunk[26:28]
        handler = {CLAIM: self.tld_claim, OWNER: self.change_owner, LISTTYPE: self.change_list_type,
                   CHANGELIST: self.change_list_content, NAME: self.change_ip_name, INSCRIBE: self.inscribe,
                   FOLDER: self.folder, FILE: self.file, PROPERTY: self.property, TOKEN: self.token}.get(act)
        if handler is not None:
            handler(block, txid, chunk, owner, output, outputs)

    @staticmethod
    def _pays(output, i: int) -> str:
        found = output(i)
        if found is None:
            raise Unreadable(f"vout[{i}] pays no address")
        return found

    # TLD/IP claim ----------------------------------------------------------------------------------------------

    def ip_claim(self, block, txid, chunk, owner, output, outputs) -> None:
        tld_h, ip_h = chunk[0:18], chunk[18:26]
        taken = allowed = False
        for t in self.TLD:
            if t["Hex"] != tld_h:
                continue
            if t["Type"] == "whitelist":
                allowed = allowed or owner in t["List"]
            if t["Type"] == "blacklist":
                allowed = owner not in t["List"]
            if t["Owner"] == owner:
                allowed = True
            taken = taken or any(i[0] == ip_h for i in t["IPs"])
            if allowed and not taken:
                t["IPs"].append([ip_h, owner, ""])
                self.FOLD.append({"Key": tld_h + ip_h, "Owner": owner, "root": {}})

    def tld_claim(self, block, txid, chunk, owner, output, outputs) -> None:
        tld_h, ip_h = chunk[0:18], chunk[18:26]
        tld_s = text(tld_h)
        if tld_h in (FIRST_TLD, LAST_TLD):
            return
        if any(t["Ascii"] == tld_s for t in self.TLD):
            self.ip_claim(block, txid, chunk, owner, output, outputs)
            return
        if ip_h != FIRST_IP:
            return
        self.LIB.append({"Block": block, "TXID": txid, "Event": "claim TLD", "Action": f"TLD {tld_s} claimed",
                         "Message": text(chunk[30:])})
        self.TLD.append({"Hex": tld_h, "Ascii": tld_s, "Owner": owner, "Block": block, "TXID": txid,
                         "Type": "whitelist", "List": [], "IPs": []})

    def change_owner(self, block, txid, chunk, owner, output, outputs) -> None:
        if outputs < 3:
            return
        new_owner, tld_h = self._pays(output, 2), chunk[0:18]
        for t in self.TLD:
            if t["Hex"] == tld_h and t["Owner"] == owner:
                t["Owner"] = new_owner
                break
        else:
            return
        self.LIB.append({"Block": block, "TXID": txid, "Event": "change owner",
                         "Action": f"TLD {text(tld_h)} owner changed", "TLD": text(tld_h)})

    def change_list_type(self, block, txid, chunk, owner, output, outputs) -> None:
        tld_h, ip_h = chunk[0:18], chunk[18:26]
        if len(chunk) != 32 or ip_h != FIRST_IP:
            return
        list_type = "whitelist" if chunk[30:32] == "00" else "blacklist"
        change = False
        for t in self.TLD:
            if t["Hex"] == tld_h and t["Owner"] == owner:
                t["Type"] = list_type
                change = True
        if change:
            self.LIB.append({"Block": block, "TXID": txid, "Event": "change type",
                             "Action": f"Type changed to {list_type}", "TLD": text(tld_h)})

    def change_list_content(self, block, txid, chunk, owner, output, outputs) -> None:
        tld_h, sub, ip_h = chunk[0:18], chunk[30:32], chunk[18:26]
        if len(chunk) != 32 or ip_h != FIRST_IP:
            return
        change = False
        if sub == "00" and outputs > 2:                     # add
            address = self._pays(output, 2)
            for t in self.TLD:
                if t["Hex"] == tld_h and t["Owner"] == owner:
                    t["List"].append(address)
                    change = True
        if sub == "01" and outputs > 2:                     # remove
            address = self._pays(output, 2)
            for t in self.TLD:
                if t["Hex"] == tld_h and t["Owner"] == owner:
                    t["List"][:] = [a for a in t["List"] if a != address]
                    change = True
        if sub == "02" and outputs > 1:                     # clear
            for t in self.TLD:
                if t["Hex"] == tld_h and t["Owner"] == owner:
                    t["List"] = []
                    change = True
        if change:
            self.LIB.append({"Block": block, "TXID": txid, "Event": "change list",
                             "Action": "List have been changed", "TLD": text(tld_h)})

    def change_ip_name(self, block, txid, chunk, owner, output, outputs) -> None:
        tld_h, ip_h, name = chunk[0:18], chunk[18:26], chunk[30:]
        if ip_h == FIRST_IP or len(chunk) < 32 or len(chunk) > 114:
            return
        for t in self.TLD:
            if t["Hex"] == tld_h:
                for i in t["IPs"]:
                    if i[0] == ip_h and i[1] == owner:
                        i[2] = text(name)

    # inscriptions, folders, files --------------------------------------------------------------------------------

    @staticmethod
    def _open_space(chunk: str, least: int) -> bool:
        tld_h, ip_h = chunk[0:18], chunk[18:26]
        return not (len(chunk) < least or ip_h in (FIRST_IP, LAST_IP) or tld_h in (FIRST_TLD, LAST_TLD))

    def inscribe(self, block, txid, chunk, owner, output, outputs) -> None:
        if not self._open_space(chunk, 42):
            return
        if chunk[30:32] != "01":                            # 00: a chunk of data, kept by nobody
            return
        meta = parse_meta(chunk[34:])
        if meta.get("next") is False:
            # A meta file over 10 kB is split, linked by prev/next; only the last one counts.
            self.LIB.append({"Block": block, "TXID": txid, "Event": "Inscription", "Action": "Meta Inscribing",
                             "TLD": text(chunk[0:18]), "IP": ip(chunk[18:26])})
            self.INS.append({"TXID": txid, "Name": meta.get("name"), "Type": meta.get("type")})

    def _space(self, chunk: str, owner: str) -> dict | None:
        key = chunk[0:18] + chunk[18:26]
        return next((e for e in self.FOLD if e["Key"] == key and e["Owner"] == owner), None)

    def folder(self, block, txid, chunk, owner, output, outputs) -> None:
        if not self._open_space(chunk, 62):
            return
        if chunk[30:32] != "00":                            # 01 delete, 02 rename, 03 move, 04 join: not built
            return
        name = text(chunk[32:62])
        if not name:
            return
        if len(chunk) == 62:
            location = "root"
        elif len(chunk) == 92:
            location = text(chunk[62:92]) or "root"
        else:
            return
        e = self._space(chunk, owner)
        if e is None:
            return
        place = e.setdefault(location, {})
        if not isinstance(place, dict):
            raise Unreadable(f"a folder put into {location!r}, which is not a folder")
        place[name] = {}
        self.LIB.append({"Block": block, "TXID": txid, "Event": "create", "Action": "Folder", "TLD": text(chunk[0:18]),
                         "IP": ip(chunk[18:26]), "Location": location, "Name": name})

    def file(self, block, txid, chunk, owner, output, outputs) -> None:
        if not self._open_space(chunk, 62):
            return
        sub, name = chunk[30:32], text(chunk[32:62])
        if sub == "00":                                     # create (name, where, content)
            if not name:
                return
            location, content = "", ""
            if len(chunk) == 62:
                location = "root"
            if len(chunk) in (92, 156):
                location = text(chunk[62:92]) or "root"
                if len(chunk) == 156:
                    content = chunk[92:156]
            e = self._space(chunk, owner)
            if e is None:
                return
            location = location or "root"
            if location == "root":
                e["root"][name] = content
            else:
                place = e["root"].setdefault(location, {})
                if not isinstance(place, dict):
                    raise Unreadable(f"a file put into {location!r}, which is a file")
                place[name] = content
            self.LIB.append({"Block": block, "TXID": txid, "Event": "create", "Action": "File",
                             "TLD": text(chunk[0:18]), "IP": ip(chunk[18:26]), "Location": location, "Name": name})
        if sub == "01":                                     # delete (name, where)
            if not name:
                return
            location = ""
            if len(chunk) == 62:
                location = "root"
            if len(chunk) == 92:
                location = text(chunk[62:92]) or "root"
            for e in self.FOLD:
                if e["Key"] != chunk[0:26] or e["Owner"] != owner:
                    continue
                for key in sorted(e["root"]):               # nlohmann's object is a std::map: in key order
                    value = e["root"][key]
                    if key == name and isinstance(value, str):
                        del e["root"][name]
                        self.LIB.append({"Block": block, "TXID": txid, "Event": "delete", "Action": "File",
                                         "TLD": text(chunk[0:18]), "IP": ip(chunk[18:26]), "Location": location,
                                         "Name": name})
                        break
                    if key == location and isinstance(value, dict):
                        break

    # properties and tokens --------------------------------------------------------------------------------------

    def property(self, block, txid, chunk, owner, output, outputs) -> None:
        if not self._open_space(chunk, 32) or chunk[30:32] != "00" or len(chunk) < 86:
            return
        define, name = chunk[32:34], text(chunk[34:76])
        ticker = text(chunk[76:86]).upper()
        if not name or not ticker:
            return
        if any(o["Name"] == name or o["Ticker"] == ticker for o in self.PROP):
            return
        kinds = {"00": (True, False, True), "01": (True, False, False), "02": (True, True, True),
                 "03": (True, True, False), "04": (False, False, False)}
        if define not in kinds:
            return
        fungible, divisible, fixed = kinds[define]
        self.LIB.append({"Block": block, "TXID": txid, "Event": "Property", "Action": "create"})
        prop = {"Name": name, "Ticker": ticker, "GrantData": "", "Owner": owner, "OwnerData": "",
                "Fungible": fungible, "Divisible": divisible, "Fixed": fixed, "Supply": 0, "Holder": {},
                "Key": chunk[0:26]}
        if fixed:
            if len(chunk) < 102:
                return
            supply = number(chunk[86:102])
            prop["Supply"] = supply
            prop["Holder"][owner] = supply
        self.PROP.append(prop)

    def token(self, block, txid, chunk, origin, output, outputs) -> None:
        if not self._open_space(chunk, 32):
            return
        sub, key = chunk[30:32], chunk[0:26]
        if sub in ("00", "01"):
            if len(chunk) < 58:
                return
            ticker, amount = text(chunk[32:42]).upper(), number(chunk[42:58])
            if len(ticker) < 3:
                return
        if sub == "00":                                     # move
            destination = self._pays(output, 2)
            for p in self.PROP:
                held = p["Holder"]
                if p["Key"] == key and p["Ticker"] == ticker and origin in held and held[origin] >= amount:
                    held[origin] -= amount
                    if held[origin] == 0:
                        del held[origin]
                    held[destination] = (held.get(destination, 0) + amount) % U64
                    self.LIB.append({"Block": block, "TXID": txid, "Event": "Token", "Action": "move"})
                    break
        if sub == "01":                                     # mint
            for p in self.PROP:
                if p["Key"] == key and p["Ticker"] == ticker and p["Owner"] == origin:
                    p["Supply"] = (p["Supply"] + amount) % U64
                    p["Holder"][origin] = (p["Holder"].get(origin, 0) + amount) % U64
                    self.LIB.append({"Block": block, "TXID": txid, "Event": "Token", "Action": "move"})


def parse_meta(data: str) -> dict:
    """A meta inscription's JSON (from byte 17), as nlohmann reads it: strict JSON in UTF-8, an object."""
    def refuse(word):
        raise ValueError(word)
    try:
        meta = json.loads(bytes.fromhex(data).decode("utf-8"), parse_constant=refuse)
    except ValueError as exc:
        raise Unreadable(f"the meta inscription is not JSON ({exc})") from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise Unreadable("the meta inscription is not a JSON object")
    if not isinstance(meta.get("next"), bool):
        raise Unreadable("the meta inscription's next is not true or false")
    return meta


# --- the document, block by block ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS undo (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, before TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skipped (height INTEGER NOT NULL, txid TEXT PRIMARY KEY, why TEXT NOT NULL) WITHOUT ROWID;
"""


class MoonBrix:
    """MoonBrix's state on one chain, in one SQLite file, followed block by block."""

    def __init__(self, path: str | Path, params) -> None:
        self.path = Path(path)
        self.params = params
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

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

    def document(self) -> dict[str, list]:
        """The five files as MoonBrix keeps them (a copy)."""
        with self.lock:
            got = self._meta("doc")
        return json.loads(got) if got else empty()

    def skipped(self) -> list[dict[str, Any]]:
        """Commands MoonBrix's own sync would stop at, passed over here (QUIRKS)."""
        return [{"height": h, "txid": t, "why": w}
                for h, t, w in self.conn.execute("SELECT height, txid, why FROM skipped ORDER BY height")]

    def reset(self) -> None:
        with self.lock, self.conn:
            for table in ("meta", "undo", "skipped"):
                self.conn.execute(f"DELETE FROM {table}")

    def _set(self, height: int, best: str, doc: dict | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('height', ?)", (str(height),))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('best', ?)", (best,))
        if doc is not None:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('doc', ?)", (json.dumps(doc),))

    def connect(self, height: int, block_hash: str, txs: Iterable[rawchain.Tx],
                spent_output: Callable[[str, int], tuple[bytes, int]]) -> bool:
        """Read block `height`'s transactions (in block order; only those carrying data are needed). Returns whether
        the document changed. `spent_output(txid, vout)` -> (script, value) of a coin made before (the Core)."""
        with self.lock:
            if self.height is not None and height <= self.height:
                raise MoonBrixError(f"block {height} is not past the document's tip {self.height}")
            doc = self.document()
            before = json.dumps(doc)
            for tx in txs:
                said = candidate(tx)
                if said is None:
                    continue
                chunk = command(said)
                if chunk is not None:
                    try:
                        self._apply(doc, height, tx, chunk, spent_output)
                    except Unreadable as exc:
                        doc = json.loads(before)
                        self.conn.execute("INSERT OR REPLACE INTO skipped VALUES (?,?,?)", (height, tx.txid, str(exc)))
                break                                       # the block's one slot is taken (Indexer.cpp)
            changed = json.dumps(doc) != before
            with self.conn:
                if changed:
                    self.conn.execute("INSERT OR REPLACE INTO undo VALUES (?,?,?)", (height, block_hash, before))
                    self.conn.execute("DELETE FROM undo WHERE height<=?", (height - UNDO_DEPTH,))
                self._set(height, block_hash, doc if changed else None)
            return changed

    def _apply(self, doc, height, tx, chunk, spent_output) -> None:
        def origin() -> str | None:
            first = tx.vin[0] if tx.vin else None
            if not first or not first.get("txid"):
                return None
            script, _value = spent_output(first["txid"], first["vout"])
            return address_of(script, self.params)

        def output(i: int) -> str | None:
            return address_of(tx.vout[i][1], self.params) if i < len(tx.vout) else None

        Kurator(doc).apply(height, tx.txid, chunk, origin, output, len(tx.vout))

    def roll_back(self, to: int, best: str) -> None:
        """Back to the document after block `to` (a reorg): whatever blocks above it changed is undone."""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT before FROM undo WHERE height>? ORDER BY height LIMIT 1", (to,)).fetchone()
            self.conn.execute("DELETE FROM undo WHERE height>?", (to,))
            self.conn.execute("DELETE FROM skipped WHERE height>?", (to,))
            if row is not None:
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('doc', ?)", (row[0],))
            self._set(to, best, None)

    def undo_heights(self) -> list[tuple[int, str]]:
        return list(self.conn.execute("SELECT height, hash FROM undo ORDER BY height DESC"))


def follow(mb: MoonBrix, uset, rpc, params) -> int:
    """Bring the document to the unspent set's tip, reading only the blocks the set says carry data. A tip that left
    the chain (a reorg) is rolled back first: to the newest block it changed that is still on the chain, or read again
    from block 0 when none is (cheap: data blocks are few). Returns how many data blocks were read."""
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
    since = 0 if mb.height is None else mb.height + 1
    look = spent_output_by_rpc(rpc)
    read = 0
    for height, txids in uset.data_blocks(since):
        if height > tip:
            break
        raws = rpc.batch([("getrawtransaction", [t, 0]) for t in txids])
        txs = [rawchain.read_tx(bytes.fromhex(raw), t, timed=params.tx_time) for t, raw in zip(txids, raws)]
        mb.connect(height, str(rpc.call("getblockhash", height)), txs, look)
        read += 1
    if mb.height != tip:
        with mb.lock, mb.conn:
            mb._set(tip, tip_hash, None)
    return read


# --- reading it -------------------------------------------------------------------------------------------------

def inscription(rpc, params, txid: str) -> tuple[str, bytes]:
    """A meta inscription's file: its type, and its chunks' data put together in order, as MoonBrix's own page does
    (Genesis.js Background: each chunk's `vout[0]` from byte 16 on)."""
    tx = rawchain.read_tx(bytes.fromhex(rpc.call("getrawtransaction", txid, 0)), txid, timed=params.tx_time)
    said = candidate(tx)
    chunk = command(said) if said else None
    if chunk is None or chunk[26:28] != INSCRIBE or chunk[30:32] != "01":
        raise KeyError(f"{txid} is not a MoonBrix meta inscription")
    meta = parse_meta(chunk[34:])
    parts = meta.get("chunks") or []
    if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
        raise KeyError(f"{txid}: its chunks are not a list of txids")
    raws = rpc.batch([("getrawtransaction", [p, 0]) for p in parts]) if parts else []
    data = []
    for part, raw in zip(parts, raws):
        piece = rawchain.read_tx(bytes.fromhex(raw), part, timed=params.tx_time)
        got = candidate(piece)
        hexs = command(got) if got else None
        if hexs is None:
            raise KeyError(f"chunk {part} carries no MoonBrix data in vout[0]")
        data.append(bytes.fromhex(hexs[32:]))
    return str(meta.get("type") or "application/octet-stream"), b"".join(data)


def balances(doc: dict[str, list], address: str) -> list[dict[str, Any]]:
    """What `address` holds of each property."""
    return [{"ticker": p["Ticker"], "name": p["Name"], "amount": p["Holder"][address], "key": p["Key"],
             "divisible": p["Divisible"], "fungible": p["Fungible"]}
            for p in doc["prop"] if address in p["Holder"]]


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
            print(json.dumps({"height": mb.height, "best": mb.best, **{k: len(v) for k, v in doc.items()},
                              "skipped": mb.skipped()}, indent=1))
    finally:
        mb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
