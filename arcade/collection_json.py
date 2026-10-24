"""Collections of inscriptions that live on another chain -- Doginals first -- described by an inscription.

2026-10-17 (asked through the Ashvale session): a collection JSON is itself an inscription; when the arcade sees
one, the collection appears in its NFT collections with no manual step. Written on PEP testnet for now, read
chain-agnostic: the root names the chain its items live on ("doge" for Doginals). His decisions:

* **First to publish.** The earliest root (block, then position) that names an item owns it; a later root naming any
  of the same items is not used ("already claimed").
* **The same publisher corrects.** A root carrying "replaces": <an earlier root's id>, inscribed by the address that
  published that earlier root, takes its place; nobody else can replace it.

A root is small; the items travel in parts of a few hundred each, so ten thousand Doginals fit:

    root  {"p": "arcade-collection", "v": 1, "name": "...", "chain": "doge", "description": "...", "cover": "<id>",
           "supply": 10000, "traits": ["Background", ...], "parts": ["<part txid>", ...],
           "sha256": "<of the items, in order, as canonical JSON>", "replaces": "<root txid>"?}
    part  {"p": "arcade-collection-items", "v": 1, "part": 1, "values": [["Blue", "Red", ...], ...],
           "items": [["<txid>i0", 1, [0, 3, ...]], ...]}

`values[t]` lists trait t's values; an item names each by its index there, or -1 for none. What a reader checks is in
`assemble`: the parts are all there and match the root's sha256, every id and every number appears once, numbers run
1..supply (gaps are reported, not refused). What the chain says about each item (it exists; no two numbers draw the
same content) is checked later, against the items' own chain (`chain_checks`).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

CONTENT_TYPE = "application/vnd.arcade.collection+json"
ROOT, PART = "arcade-collection", "arcade-collection-items"
VERSION = 1
#: Items per part: ~90 bytes each, so a part is ~45 KB -- a few transactions, not a hundred.
PER_PART = 500
CHAINS = ("doge", "main", "bel", "lky", "test", "regtest")   # bel, lky: 2026-10-23, the Creator tab's ords
ITEM_ID = re.compile(r"^[0-9a-f]{64}i[0-9]{1,6}$")
TXID = re.compile(r"^[0-9a-f]{64}$")


class CollectionError(ValueError):
    """Not a collection this arcade can show, and why."""


@dataclass
class Item:
    id: str
    number: int
    traits: dict[str, str] = field(default_factory=dict)


@dataclass
class Collection:
    name: str
    chain: str
    supply: int
    traits: list[str]
    items: list[Item]
    description: str = ""
    cover: str = ""
    replaces: str = ""
    missing: list[int] = field(default_factory=list)       # numbers 1..supply nobody claims


def _canonical(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def items_digest(rows: list[list]) -> str:
    """The sha256 a root carries: of every item row, in order, as compact canonical JSON."""
    return hashlib.sha256(_canonical(rows)).hexdigest()


# --- writing (the converter) ------------------------------------------------------------------------------------------

def build(name: str, chain: str, items: list[Item], traits: list[str], description: str = "", cover: str = "",
          replaces: str = "", per_part: int = PER_PART) -> tuple[dict, list[dict]]:
    """(root without "parts", the parts) for a list of items. The root's "parts" are filled in once each part is
    inscribed and has an id (`with_parts`)."""
    if chain not in CHAINS:
        raise CollectionError(f"a collection's items live on one of {', '.join(CHAINS)}")
    items = sorted(items, key=lambda i: i.number)
    values = [sorted({i.traits.get(t, "") for i in items} - {""}) for t in traits]
    index = [{v: n for n, v in enumerate(vs)} for vs in values]
    rows = [[i.id, int(i.number), [index[t].get(i.traits.get(name_, ""), -1) for t, name_ in enumerate(traits)]]
            for i in items]
    _check_rows(rows, traits)
    parts = []
    for n in range(0, len(rows), per_part):
        chunk = rows[n:n + per_part]
        # Each part carries only the values its own items use, so a part reads alone.
        used = [sorted({r[2][t] for r in chunk if r[2][t] >= 0}) for t in range(len(traits))]
        remap = [{old: new for new, old in enumerate(u)} for u in used]
        parts.append({"p": PART, "v": VERSION, "part": len(parts) + 1,
                      "values": [[values[t][i] for i in used[t]] for t in range(len(traits))],
                      "items": [[r[0], r[1], [remap[t].get(r[2][t], -1) for t in range(len(traits))]] for r in chunk]})
    root = {"p": ROOT, "v": VERSION, "name": name, "chain": chain, "supply": max((i.number for i in items), default=0),
            "traits": list(traits), "sha256": items_digest(_rows_by_value(parts, traits))}
    if description:
        root["description"] = description
    if cover:
        root["cover"] = cover
    if replaces:
        root["replaces"] = replaces
    return root, parts


def with_parts(root: dict, part_ids: list[str]) -> dict:
    return {**root, "parts": list(part_ids)}


def from_dm(rows: list[dict]) -> tuple[list[Item], list[str]]:
    """doggy.market's layout: [{"inscriptionId", "name": "X #n", "attributes": {trait: value}}]."""
    items, traits = [], []
    for r in rows:
        attrs = r.get("attributes") or {}
        for t in attrs:
            if t not in traits:
                traits.append(t)
        items.append(Item(id=str(r["inscriptionId"]), number=_number_of(r.get("name", "")),
                          traits={k: str(v) for k, v in attrs.items()}))
    return items, traits


def from_ow(rows: list[dict]) -> tuple[list[Item], list[str]]:
    """The OW layout: [{"id", "meta": {"name": "X #n", "attributes": [{"trait_type", "value"}]}}]."""
    items, traits = [], []
    for r in rows:
        meta = r.get("meta") or {}
        attrs = {str(a.get("trait_type")): str(a.get("value")) for a in meta.get("attributes") or []}
        for t in attrs:
            if t not in traits:
                traits.append(t)
        items.append(Item(id=str(r["id"]), number=_number_of(meta.get("name", "")), traits=attrs))
    return items, traits


def _number_of(name: str) -> int:
    found = re.search(r"#\s*(\d+)\s*$", str(name))
    if not found:
        raise CollectionError(f"{name!r} does not end in its number (\"Name #12\")")
    return int(found.group(1))


# --- reading ----------------------------------------------------------------------------------------------------------

def parse_root(body: bytes | str) -> dict:
    said = _json(body)
    if said.get("p") != ROOT or said.get("v") != VERSION:
        raise CollectionError("not a collection root")
    name = said.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise CollectionError("a collection has a name, up to 80 characters")
    if said.get("chain") not in CHAINS:
        raise CollectionError("a collection names the chain its items live on")
    if not isinstance(said.get("supply"), int) or not 0 < said["supply"] <= 1_000_000:
        raise CollectionError("a collection's supply is a whole number")
    traits = said.get("traits") or []
    if not isinstance(traits, list) or not all(isinstance(t, str) for t in traits) or len(set(traits)) != len(traits):
        raise CollectionError("traits are distinct names")
    parts = said.get("parts") or []
    if not parts or not all(isinstance(p, str) and TXID.match(p) for p in parts):
        raise CollectionError("a root lists its parts by transaction id")
    if not re.fullmatch(r"[0-9a-f]{64}", str(said.get("sha256", ""))):
        raise CollectionError("a root carries the sha256 of its items")
    if said.get("replaces") and not TXID.match(str(said["replaces"])):
        raise CollectionError("replaces names the root it replaces, by transaction id")
    return said


def parse_part(body: bytes | str, traits: list[str]) -> dict:
    said = _json(body)
    if said.get("p") != PART or said.get("v") != VERSION:
        raise CollectionError("not a part of a collection")
    values = said.get("values")
    if not isinstance(values, list) or len(values) != len(traits):
        raise CollectionError("a part lists every trait's values")
    items = said.get("items")
    if not isinstance(items, list):
        raise CollectionError("a part lists its items")
    return said


def assemble(root: dict, parts: list[dict]) -> Collection:
    """The collection a root and its parts describe, or CollectionError. Refuses duplicate ids and duplicate numbers
    outright (the Anti Doginal Dogs trait file had five duplicate rows; a market listed two #3099s); reports gaps."""
    traits = root["traits"]
    if len(parts) != len(root["parts"]):
        raise CollectionError("a part is missing")
    rows = _rows_by_value(parts, traits)
    if items_digest(rows) != root["sha256"]:
        raise CollectionError("the parts are not the ones the root was written over (sha256 differs)")
    _check_rows([[r[0], r[1], [0] * len(traits)] for r in rows], traits)
    items = [Item(id=r[0], number=r[1], traits={t: v for t, v in zip(traits, r[2]) if v}) for r in rows]
    have = {i.number for i in items}
    if max(have, default=0) > root["supply"]:
        raise CollectionError("an item's number is above the supply")
    return Collection(name=root["name"].strip(), chain=root["chain"], supply=root["supply"], traits=traits,
                      items=items, description=str(root.get("description", ""))[:2000],
                      cover=str(root.get("cover", "")), replaces=str(root.get("replaces", "")),
                      missing=[n for n in range(1, root["supply"] + 1) if n not in have])


def _rows_by_value(parts: list[dict], traits: list[str]) -> list[list]:
    """Every item as [id, number, [trait value or ""]], in part order: what the sha256 is taken over."""
    rows = []
    for part in parts:
        values = part["values"]
        for item in part["items"]:
            if (not isinstance(item, list) or len(item) != 3 or not isinstance(item[2], list)
                    or len(item[2]) != len(traits)):
                raise CollectionError("an item is [id, number, [a value per trait]]")
            names = []
            for t, at in enumerate(item[2]):
                if not isinstance(at, int) or at < -1 or at >= len(values[t]):
                    raise CollectionError(f"item {item[0]} names a {traits[t]} value that is not listed")
                names.append("" if at < 0 else str(values[t][at]))
            rows.append([item[0], item[1], names])
    return rows


def _check_rows(rows: list[list], traits: list[str]) -> None:
    ids, numbers = set(), set()
    for r in rows:
        if not isinstance(r[0], str) or not ITEM_ID.match(r[0]):
            raise CollectionError(f"{r[0]!r} is not an inscription id (<txid>i<n>)")
        if not isinstance(r[1], int) or r[1] < 1:
            raise CollectionError(f"{r[0]}: an item's number is 1 or more")
        if r[0] in ids:
            raise CollectionError(f"{r[0]} is listed twice")
        if r[1] in numbers:
            raise CollectionError(f"two items are numbered {r[1]}")
        ids.add(r[0])
        numbers.add(r[1])


def _json(body: bytes | str) -> dict:
    try:
        said = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise CollectionError("not JSON") from None
    if not isinstance(said, dict):
        raise CollectionError("not a JSON object")
    return said


# --- what the chain says ----------------------------------------------------------------------------------------------

def chain_checks(items: list[Item], content_hash: Callable[[str], str | None]) -> dict[str, list]:
    """Against the items' own chain: `content_hash(id)` is the sha256 of an item's content, or None when the chain
    has no such inscription. Returns {"missing": [ids], "same_content": [[number, number], ...]} -- two numbers that
    draw the same thing (Anti Doginal Dogs #8212 was a second copy of #8203)."""
    missing, seen, same = [], {}, []
    for item in items:
        digest = content_hash(item.id)
        if digest is None:
            missing.append(item.id)
        elif digest in seen:
            same.append([seen[digest], item.number])
        else:
            seen[digest] = item.number
    return {"missing": missing, "same_content": same}
