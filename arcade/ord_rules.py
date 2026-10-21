"""The rules an ord index keeps, read from the indexers people use (2026-10-20: "I'm pretty sure all ORD's are
included in the index. Why don't you check the repo?", then DRC-20 and PRC-20 balances, then the names).

* Inscriptions: as wonky-ord (tylordius/wonky-dogeord) and apezord/ord-dogecoin read a Doginals envelope --
  src/inscription.rs `InscriptionParser` and src/index/updater/inscription_updater.rs. Only input 0's script, only
  push operations, "ord", a piece count above 0, the content type, then (countdown, data) pairs counting down exactly;
  a body spread over several transactions continues in the one whose input 0 spends the previous one. Copied with its
  quirk: OP_PUSHDATA2/4 lengths read the opcode byte as the low byte, so a push over 255 bytes is misread (Doginals
  use 240-byte pieces, so ordinary inscriptions never meet it) -- the official numbering depends on it.
* DRC-20 (Dogecoin) and PRC-20 (Pepecoin, reallyshadydev/pepecoin-ord-indexer): src/drc20/* and
  src/index/updater/drc20_updater.rs -- deploy, mint, inscribe-transfer, transfer.
* Names (`"p":"dns"`): three readings that disagree (see NAME_RULES), every candidate kept so the disagreements show.
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

RULES = 2                      # bump when what an index keeps changes: an index of other rules is cut again


# --------------------------------------------------------------------------- inscriptions (wonky-ord)

def decode_push_datas(script: bytes) -> list[bytes] | None:
    """InscriptionParser::decode_push_datas: every operation must be a push, else None."""
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
        elif op == 77:                                        # the indexers' quirk: the opcode byte as the low byte
            if len(b) < 3:
                return None
            n = (b[1] << 8) + b[0]
            if len(b) < 3 + n:
                return None
            out.append(b[3:3 + n])
            b = b[3 + n:]
        elif op == 78:
            if len(b) < 5:
                return None
            n = (b[3] << 24) + (b[2] << 16) + (b[1] << 8) + b[0]
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


def parse(scripts: list[bytes]) -> tuple[str, bytes, bytes]:
    """("none" | "partial" | "complete", content type, body) for input 0's scripts of a chain of transactions
    (InscriptionParser::parse)."""
    pushes = decode_push_datas(scripts[0])
    if pushes is None or len(pushes) < 3 or pushes[0] != b"ord":
        return "none", b"", b""
    npieces = push_number(pushes[1])
    if not npieces:
        return "none", b"", b""
    ctype, rest, body = pushes[2], pushes[3:], b""
    left = list(scripts)
    while True:
        while True:
            if npieces == 0:
                return "complete", ctype, body
            if len(rest) < 2:
                break
            nxt = push_number(rest[0])
            if nxt is None or nxt != npieces - 1:
                break
            body += rest[1]
            rest = rest[2:]
            npieces -= 1
        if len(left) <= 1:
            return "partial", ctype, body
        left = left[1:]
        rest = decode_push_datas(left[0])
        if rest is None or len(rest) < 2:
            return "none", b"", b""
        nxt = push_number(rest[0])
        if nxt is None or nxt != npieces - 1:
            return "none", b"", b""


def genesis(script0: bytes) -> tuple[str, bytes] | None:
    """A transaction that STARTS an inscription, read alone: (state, content type) or None. What a helper can see
    without knowing anything earlier (a continuation never starts with "ord")."""
    state, ctype, _ = parse([script0])
    return None if state == "none" else (state, ctype)


def protocol_of(body: bytes) -> str | None:
    """The `"p"` of a JSON body ("drc-20", "dns", ...), kept with every inscription so a protocol indexed later finds
    its own inscriptions without reading every body again (2026-10-20). None when the body is not such JSON."""
    head = body.lstrip()[:1]
    if head != b"{" or len(body) > 65536:
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
