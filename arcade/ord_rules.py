"""The rules an ord index keeps, read from the indexers people use (2026-10-20: "I'm pretty sure all ORD's are
included in the index. Why don't you check the repo?", then DRC-20 and PRC-20 balances, then the names).

* Inscriptions: as wonky-ord (tylordius/wonky-dogeord) and apezord/ord-dogecoin read a Doginals envelope --
  src/inscription.rs `InscriptionParser` and src/index/updater/inscription_updater.rs. Only input 0's script, only
  push operations, "ord", a piece count above 0, the content type, then (countdown, data) pairs counting down exactly;
  a body spread over several transactions continues in the one whose input 0 spends the previous one. Copied with its
  quirk: OP_PUSHDATA2/4 lengths read the opcode byte as the low byte, so a push over 255 bytes is misread (Doginals
  use 240-byte pieces, so ordinary inscriptions never meet it) -- the official numbering depends on it.
* Two dialects of that envelope (2026-10-21, the operator: "be exact and follow the official indexers' source to the
  letter"), chosen by the chain (`dialect_of`):
  - "dogecoin" (Dogecoin, and the Doginals kind on Litecoin): wonky-ord src/inscription.rs as above. After the
    countdown reaches 0, the rest of THAT script's pushes are fields: pairs whose key is exactly 1 byte, read up to
    the first key of another length (`for item in push_datas.chunks(2) { [key, value] => if key.len() != 1 { break }
    ...`); `Tag::Delegate.take(&mut fields)` is the first value of key 11 (src/tag.rs `Delegate = 11`), an id as
    `inscription_id_field` reads it (32 bytes of txid as stored, 0-4 bytes of index little-endian, no trailing zero
    unless all 4 are there).
  - "pepecoin" (Pepecoin main and test): mvdnbrk/ord-pepecoin src/inscriptions/parser.rs. A piece count of 0 is a
    COMPLETE inscription with no body (`if npieces == 0 { ... ParsedInscription::Complete(Inscription { body: None,
    tags }) }`); after the countdown reaches 0 the rest of the pushes -- of that script and of every later script in
    the chain -- are the PRC-721 tag trailer (`parse_tags`: `chunks_exact(2)`, a key that is UTF-8 kept, every value
    of a key kept in order); src/inscriptions/tag.rs: "parent" and "delegate" values are 36 bytes, the txid as stored
    (reversed from how it is written) + the index as 4 bytes little-endian (`parse_inscription_id`; any other length is
    no id). Its decode_push_datas reads OP_PUSHDATA2/4 lengths correctly (fixed in 414ce571, 2026-03-10: "OP_PUSHDATA2/4
    parser bug"), so on Pepecoin a push over 255 bytes is read, not misread.
* DRC-20 (Dogecoin) and PRC-20 (Pepecoin, reallyshadydev/pepecoin-ord-indexer): src/drc20/* and
  src/index/updater/drc20_updater.rs -- deploy, mint, inscribe-transfer, transfer.
* Names (`"p":"dns"`): three readings that disagree (see NAME_RULES), every candidate kept so the disagreements show.
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

RULES = 4                      # bump when what an index keeps changes: an index of other rules is cut again
#: A chain whose index rules moved on alone, so the other chains' indexes are not cut again for it: Dogecoin 5
#: (2026-10-22, the operator: number reinscriptions "like nintondo" -- a second inscription on an inscribed coin is numbered;
#: wonky-ord / ord-dogecoin never numbered one, so every Dogecoin number after the first differs; Litecoin, which also
#: reads the Dogecoin dialect, keeps 4).
RULES_BY_NET = {"doge": 5}


def rules_for(net: str) -> int:
    """The rules version of `net`'s ord index (RULES unless RULES_BY_NET says otherwise)."""
    return RULES_BY_NET.get(net or "", RULES)
#: 3 (2026-10-21): an envelope is no longer dropped when a transaction spends one of its pieces without continuing it
#: (doginal_index.Walker: the official indexers keep it) -- every index walked under 2 lost such inscriptions.
#: 4 (2026-10-21): each chain read in its own indexer's dialect (DIALECTS). On Pepecoin a piece count of 0 is an
#: inscription (ord-pepecoin numbers it, so every later number was low under 3), a new inscription is looked for in
#: every transaction (inscription_updater.rs `if true`, not only when no inscription sits on the first koinu), a push
#: over 255 bytes is read; every chain keeps each inscription's delegate (and on Pepecoin its parent). Every index is
#: walked again -- births, numbers and the notes change on Pepecoin.

#: The envelope readings (see the module's docstring), and which chain reads which.
DIALECTS = ("dogecoin", "pepecoin")
PEPECOIN_NETS = frozenset({"main", "test"})


def dialect_of(net: str) -> str:
    """The dialect a chain's ord index reads: its own official indexer's (ord-pepecoin on Pepecoin main and test;
    wonky-ord / ord-dogecoin on Dogecoin, and on Litecoin's Doginals kind -- whose numbering this leaves as it was)."""
    return "pepecoin" if net in PEPECOIN_NETS else "dogecoin"


# --------------------------------------------------------------------------- inscriptions (wonky-ord)

def decode_push_datas(script: bytes, dialect: str = "dogecoin") -> list[bytes] | None:
    """InscriptionParser::decode_push_datas: every operation must be a push, else None. "dogecoin" keeps wonky-ord's
    OP_PUSHDATA2/4 quirk; "pepecoin" reads their lengths as ord-pepecoin does (`(bytes[1] as usize) + ((bytes[2] as
    usize) << 8)`)."""
    right = dialect == "pepecoin"
    out: list[bytes] = []
    b = script
    while b:
        op = b[0]
        if op == 0:
            out.append(b"")
            b = b[1:]
        elif 81 <= op <= 96:
            out.append(bytes([op - 80]))
            b = b[1:]
        elif 1 <= op <= 75:
            if len(b) < 1 + op:
                return None
            out.append(b[1:1 + op])
            b = b[1 + op:]
        elif op == 76:
            if len(b) < 2 or len(b) < 2 + b[1]:
                return None
            n = b[1]
            out.append(b[2:2 + n])
            b = b[2 + n:]
        elif op == 77:                                        # wonky-ord's quirk: the opcode byte as the low byte
            if len(b) < 3:
                return None
            n = b[1] + (b[2] << 8) if right else (b[1] << 8) + b[0]
            if len(b) < 3 + n:
                return None
            out.append(b[3:3 + n])
            b = b[3 + n:]
        elif op == 78:
            if len(b) < 5:
                return None
            n = b[1] + (b[2] << 8) + (b[3] << 16) + (b[4] << 24) if right else (b[3] << 24) + (b[2] << 16) + (b[1] << 8) + b[0]
            if len(b) < 5 + n:
                return None
            out.append(b[5:5 + n])
            b = b[5 + n:]
        else:
            return None
    return out


def push_number(data: bytes) -> int | None:
    if len(data) > 8:
        return None
    return int.from_bytes(data, "little") if data else 0


def parse(scripts: list[bytes], dialect: str = "dogecoin") -> tuple[str, bytes, bytes]:
    """("none" | "partial" | "complete", content type, body) for input 0's scripts of a chain of transactions
    (InscriptionParser::parse). A zero-piece Pepecoin inscription's body is b"" (ord-pepecoin's `body: None`)."""
    state, ctype, body, _tags = read(scripts, dialect)
    return state, ctype, body


def read(scripts: list[bytes], dialect: str = "dogecoin") -> tuple[str, bytes, bytes, dict]:
    """parse, with the complete inscription's tags (Pepecoin: {"delegate": [values], "parent": [...], ...}; Dogecoin:
    {b"\\x0b": [values], ...}) -- {} unless complete. On Pepecoin the trailer runs on through every later script given
    (parser.rs: "Collect PRC-721 tag trailer from remaining push data in this tx and all subsequent txs in the
    chain"; a script that is not push-only adds nothing). The walk never meets a later script: inscription_updater.rs
    parses the pieces kept so far plus the one transaction at hand, and a chain is kept only while it is partial, so
    the countdown always ends in the last script -- which is why envelope_start / envelope_more read the trailer of the
    script it ends in, and nothing after."""
    got, rest = _start(scripts[0], dialect)
    k = 0
    while got[0] == "partial" and k + 1 < len(scripts):
        k += 1
        got, rest = _more(got, scripts[k], dialect)
    if got[0] != "complete":
        return got[0], got[1], got[3], {}
    if dialect == "pepecoin":
        for script in scripts[k + 1:]:
            more = decode_push_datas(script, dialect)
            if more is not None:
                rest = rest + more
    return got[0], got[1], got[3], _tags(rest, dialect)


#: An envelope being read a piece at a time: (state, content type, pieces still to come, body so far, tags). The walk
#: keeps one of these for a body still arriving, never the pieces themselves (2026-10-20: keeping every piece's script,
#: and copying it into every block's undo, made PEP Main's index 6 GB bigger than it is). Tags ({} until complete):
#: what the trailer of the script it completes in says (`_tags`).
Envelope = tuple[str, bytes, int, bytes, dict]
_NONE: Envelope = ("none", b"", 0, b"", {})


def _pieces(rest: list[bytes], npieces: int, body: bytes) -> tuple[int, bytes, list[bytes]]:
    """Take the countdown pieces off one script's pushes, in order, while they count down: (pieces still to come,
    body, the pushes left over -- the trailer, once none are to come)."""
    while npieces and len(rest) >= 2:
        nxt = push_number(rest[0])
        if nxt is None or nxt != npieces - 1:
            break
        body += rest[1]
        rest = rest[2:]
        npieces -= 1
    return npieces, body, rest


def _tags(rest: list[bytes], dialect: str) -> dict:
    """The pushes after the countdown, as the dialect reads them.

    pepecoin -- parser.rs `parse_tags`: `for pair in push_datas.chunks_exact(2) { if let Ok(key) =
    str::from_utf8(&pair[0]) { tags.entry(key.to_string()).or_default().push(pair[1].clone()) } }` (a key that is
    not UTF-8 skips its pair; an odd push at the end is left).
    dogecoin -- inscription.rs: `for item in push_datas.chunks(2) { match item { [key, value] => { if key.len() != 1
    { break; } fields.entry(key).or_default().push(value) } _ => {} } }`."""
    out: dict = {}
    if dialect == "pepecoin":
        for i in range(0, len(rest) - 1, 2):
            try:
                key = rest[i].decode("utf-8")
            except UnicodeDecodeError:
                continue
            out.setdefault(key, []).append(rest[i + 1])
        return out
    for i in range(0, len(rest) - 1, 2):
        if len(rest[i]) != 1:
            break
        out.setdefault(rest[i], []).append(rest[i + 1])
    return out


def _start(script0: bytes, dialect: str) -> tuple[Envelope, list[bytes]]:
    pushes = decode_push_datas(script0, dialect)
    if pushes is None or len(pushes) < 3 or pushes[0] != b"ord":
        return _NONE, []
    npieces = push_number(pushes[1])
    if npieces is None:
        return _NONE, []
    if npieces == 0:
        # ord-pepecoin: `if npieces == 0 { ... return ParsedInscription::Complete(Inscription { content_type:
        # Some(content_type), body: None, tags }) }`; wonky-ord: `if npieces == 0 { return ParsedInscription::None; }`
        if dialect == "pepecoin":
            return ("complete", pushes[2], 0, b"", {}), pushes[3:]
        return _NONE, []
    left, body, rest = _pieces(pushes[3:], npieces, b"")
    return (("complete", pushes[2], 0, body, {}), rest) if left == 0 else (("partial", pushes[2], left, body, {}), [])


def _more(env: tuple, script: bytes, dialect: str) -> tuple[Envelope, list[bytes]]:
    ctype, npieces, body = env[1], env[2], env[3]
    rest = decode_push_datas(script, dialect)
    if rest is None or len(rest) < 2:
        return _NONE, []
    nxt = push_number(rest[0])
    if nxt is None or nxt != npieces - 1:
        return _NONE, []
    left, body, rest = _pieces(rest, npieces, body)
    return (("complete", ctype, 0, body, {}), rest) if left == 0 else (("partial", ctype, left, body, {}), [])


def envelope_start(script0: bytes, dialect: str = "dogecoin") -> Envelope:
    """The envelope a chain's first transaction starts (its input 0's script)."""
    env, rest = _start(script0, dialect)
    return env if env[0] != "complete" else env[:4] + (_tags(rest, dialect),)


def envelope_more(env: tuple, script: bytes, dialect: str = "dogecoin") -> Envelope:
    """A partial envelope ((state, content type, pieces to come, body[, tags])) and the next transaction's input 0
    script: its next state. A continuation must go on counting down where the last one stopped."""
    got, rest = _more(env, script, dialect)
    return got if got[0] != "complete" else got[:4] + (_tags(rest, dialect),)


def id_from_tag(value: bytes | None, dialect: str) -> str | None:
    """An inscription id ("<txid>i<n>", the txid as written) from a tag's value, or None.

    pepecoin -- tag.rs `parse_inscription_id`: `if value.len() != 36 { return None; }`, `Txid::from_slice(&value[0..32])`
    (the txid as stored: reversed from how it is written), `u32::from_le_bytes(value[32..36])`.
    dogecoin -- inscription.rs `inscription_id_field` (ord's): 32 to 36 bytes; the index 0-4 bytes little-endian,
    `if index.len() != 4 && *last == 0 { return None; }`."""
    if value is None:
        return None
    if dialect == "pepecoin":
        if len(value) != 36:
            return None
        return f"{value[:32][::-1].hex()}i{int.from_bytes(value[32:36], 'little')}"
    if not 32 <= len(value) <= 36:
        return None
    index = value[32:]
    if index and len(index) != 4 and index[-1] == 0:
        return None
    return f"{value[:32][::-1].hex()}i{int.from_bytes(index, 'little') if index else 0}"


#: The tag that names an inscription's delegate, per dialect (ord-pepecoin tag.rs DELEGATE; wonky-ord tag.rs
#: `Delegate = 11`), and its parent (ord-pepecoin PARENT; wonky-ord's `Parent = 3` is never read there).
DELEGATE_TAG = {"pepecoin": "delegate", "dogecoin": b"\x0b"}
PARENT_TAG = {"pepecoin": "parent"}


def delegate_of(tags: dict, dialect: str) -> str | None:
    """The delegate an inscription's tags name: the FIRST value only, as both read it (ord-pepecoin `delegate_id`:
    `.get(tag::DELEGATE).and_then(|values| values.first()).and_then(parse_inscription_id)`; wonky-ord
    `Tag::Delegate.take` takes the first value) -- a first value that is no id is no delegate, a later one is not
    tried."""
    values = tags.get(DELEGATE_TAG[dialect]) or []
    return id_from_tag(values[0], dialect) if values else None


def parents_of(tags: dict, dialect: str) -> list[str]:
    """Every parent an inscription's tags claim, in order, each that reads as an id (inscription_updater.rs: `for
    parent_id_bytes in inscription.tags.get("parent")... if let Some(parent_id) = parse_inscription_id(...)`). Only
    Pepecoin reads parents; whether a claim stands (the parent spent in the same transaction) the walk decides."""
    key = PARENT_TAG.get(dialect)
    if key is None:
        return []
    return [p for p in (id_from_tag(v, dialect) for v in tags.get(key) or []) if p is not None]


#: The body bytes that matter to the rules: a JSON "p" is read from a body of at most this many bytes.
PROTOCOL_MAX = 65536


def body_matters(ctype: bytes, body: bytes, size: int) -> bool:
    """Could anything the index keeps still depend on a body arriving in pieces, beyond its size? A name claim reads
    a text/plain or application/json body; protocol_of reads a JSON "p" from any body that opens with "{" and stays
    within PROTOCOL_MAX bytes. Tokens never arrive in pieces (single-transaction inscriptions only). When this is
    False the body is let go of, for good -- the content itself is read from the chain when it is wanted."""
    t = ctype.decode("utf-8", "replace").split(";")[0].strip().lower()
    if t in ("text/plain", "application/json"):
        return True
    return size <= PROTOCOL_MAX and body.lstrip()[:1] in (b"", b"{")


def genesis(script0: bytes, dialect: str = "dogecoin") -> tuple[str, bytes] | None:
    """A transaction that STARTS an inscription, read alone: (state, content type) or None. What a helper can see
    without knowing anything earlier (a continuation never starts with "ord"). On Pepecoin a piece count of 0 is a
    complete one."""
    state, ctype, _ = parse([script0], dialect)
    return None if state == "none" else (state, ctype)


def protocol_of(body: bytes) -> str | None:
    """The `"p"` of a JSON body ("drc-20", "dns", ...), kept with every inscription so a protocol indexed later finds
    its own inscriptions without reading every body again (2026-10-20). None when the body is not such JSON."""
    head = body.lstrip()[:1]
    if head != b"{" or len(body) > PROTOCOL_MAX:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    p = value.get("p") if isinstance(value, dict) else None
    return p[:32] if isinstance(p, str) and p else None


# --------------------------------------------------------------------------- DRC-20 / PRC-20 (wonky-ord src/drc20)

MAX_DECIMALS = 18
MAX_SUPPLY = 2 ** 64 - 1                 # u64::MAX, before the decimals are applied


class TokenError(ValueError):
    pass


def _num(s) -> Decimal:
    """Num::from_str: no leading or trailing ".", no exponent or sign, at most 18 decimals."""
    if not isinstance(s, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)?", s):
        raise TokenError(f"not a number: {s!r}")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise TokenError(f"not a number: {s!r}") from None
    if not d.is_finite():
        raise TokenError(f"not a number: {s!r}")
    if _scale(d) > MAX_DECIMALS:
        raise TokenError(f"too many decimals: {s!r}")
    return d


def _scale(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _units(d: Decimal, dec: int) -> int:
    v = d * (Decimal(10) ** dec)
    if v != v.to_integral_value():
        raise TokenError("not a whole number of units")
    return int(v)


def token_op(protocol: str, ctype: bytes, body: bytes) -> dict | None:
    """The operation an inscription's content says, or None (operation.rs deserialize_drc20_operation)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 40:
        return None
    t = ctype.decode("utf-8", "replace")
    if not (t.startswith("text/plain") or t.startswith("application/json")):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("p") != protocol:
        return None
    op = value.get("op")
    tick = value.get("tick")
    if op not in ("deploy", "mint", "transfer") or not isinstance(tick, str):
        return None
    if len(tick.encode("utf-8")) != 4:
        return None                                   # Tick: exactly 4 bytes
    if op == "deploy":
        if not isinstance(value.get("max"), str):
            return None
        for k in ("lim", "dec"):
            if k in value and value[k] is not None and not isinstance(value[k], str):
                return None
        return {"op": "deploy", "tick": tick, "max": value["max"], "lim": value.get("lim"), "dec": value.get("dec")}
    if not isinstance(value.get("amt"), str):
        return None
    return {"op": op, "tick": tick, "amt": value["amt"]}


# --------------------------------------------------------------------------- names ("p":"dns")

#: The readings of a name registration, each kept so their disagreements show (2026-10-20: "I don't know for
#: sure if Doge soft indexes domain correctly"). Same order everywhere: the first valid claim of a name wins, by
#: inscription number; the owner is whoever holds that inscription now.
NAME_RULES = {
    "spec": "DNS v1.2 (jonheaven/dns): plain text label.namespace or JSON reg/register; any namespace; Unicode rules",
    "json": "JSON only (p:dns, op:reg), any ending, older special characters allowed -- as the operator's "
            "registrar sells them",
    "doge2023": "The 2023 indexer guide: JSON p:dns op:reg, lowercase, first word, exactly one dot, .doge only",
}


def name_claim(ctype: bytes, body: bytes) -> tuple[str, str] | None:
    """(kind, raw name) when an inscription claims a name: kind "json" ({"p":"dns","op":"reg"|"register"}) or "text"
    (its first word looks like label.namespace)."""
    t = ctype.decode("utf-8", "replace").split(";")[0].strip().lower()
    if t not in ("text/plain", "application/json"):
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    s = text.strip()
    if s.startswith("{"):
        try:
            value = json.loads(re.sub(r",\s*}\s*$", "}", s))
        except ValueError:
            return None
        if isinstance(value, dict) and value.get("p") == "dns" and value.get("op") in ("reg", "register") \
                and isinstance(value.get("name"), str):
            return "json", value["name"]
        return None
    first = s.split(None, 1)[0] if s else ""
    return ("text", first) if "." in first else None


_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}


def _script(ch: str) -> str:
    try:
        return unicodedata.name(ch).split(" ")[0]
    except ValueError:
        return "?"


def _spec_part(part: str) -> bool:
    if not 1 <= len(part) <= 63 or part.startswith("-") or part.endswith("-"):
        return False
    scripts = set()
    for ch in part:
        if ch.isascii():
            if not re.fullmatch(r"[a-z0-9-]", ch):
                return False
            if ch.isalpha():
                scripts.add("LATIN")
            continue
        cat = unicodedata.category(ch)
        if ch in ("‍", "️", "︎") or cat.startswith("M") or cat == "So" or cat.startswith("N"):
            continue
        if not cat.startswith("L"):
            return False
        scripts.add(_script(ch))
    return len(scripts) <= 1


def normalise(rule: str, kind: str, raw: str) -> str | None:
    """The name a claim makes under one reading, or None when that reading does not count it."""
    if rule == "spec":
        name = raw.translate(_FULLWIDTH).lower()
        parts = name.split(".")
        if len(parts) != 2 or not all(_spec_part(p) for p in parts):
            return None
        if parts[1] in ("bitmap",) or parts[1].startswith("dogemap"):
            return None
        return name
    if kind != "json":
        return None
    if rule == "json":
        name = raw.strip().lower()
        label, dot, ending = name.rpartition(".")
        return name if dot and label and ending and not re.search(r"\s", name) else None
    if rule == "doge2023":
        name = raw.lower()
        name = name.split()[0] if name.split() else ""
        name = name.strip()
        return name if name.count(".") == 1 and name.endswith(".doge") and len(name) > 5 else None
    return None
