"""The nodes globe: which list it shows, and the city-level feed it reads.

Admin → Overview chooses the list (`globe_list` in settings). The public page
`/globe` polls `/globe.json` and redraws when that choice or its membership
changes, so nobody rebuilds the HTML to add a member.

What the feed is allowed to say, and nothing else:

* a display name (an operator @tag)
* a coarse place, city or country, and only one this file already knows
* whether that member has a node linked now
* how many of their individual arcades are linked
* the metal that count earns

No host, no IP, no mesh id, no Cloudflare address, and no lat/lon that did not
come from the coarse-place table below. A name or place that looks like an
address is dropped. A member with no known place is still listed, unplaced,
rather than plotted on a guessed town.

Tiers. `C` is how many chains the active arcades on the chosen list are
actually running (at least 1). The metals widen with `C`:

    copper   1 .. C
    bronze   C+1 .. 2C
    silver   2C+1 .. 3C
    gold     3C+1 and up

Any number of members can share a tier. Diamond is everyone tied for the most
active nodes, and it wins over the metal that same count would otherwise earn.
Copper, bronze, silver, and gold are the bands above, shared by everyone in
them. A member with no node linked is inactive and has no tier.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

SETTING = "globe_list"
SOURCES = ("mesh", "linked", "picked")
TIERS = ("copper", "bronze", "silver", "gold", "diamond")

_IP = re.compile(r"(?:^|[^0-9])(?:\d{1,3}\.){3}\d{1,3}(?:[^0-9]|$)")
_IPV6 = re.compile(r"\[[0-9a-fA-F:]+\]|(?:^|[^0-9a-f:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]*", re.I)
_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)
_HEX = re.compile(r"^[0-9a-f]{8,}$", re.I)
_TAG = re.compile(r"^[a-z0-9_]{2,24}$")
_CHAIN = re.compile(r"^[a-z0-9]{2,16}$")

#: Coarse places already on the globe. Coordinates belong to the label, never
#: to a host. Two operators may share one city. An empty tag is only a place
#: the operator can assign; it is not a person.
PLACES = (
    {"tag": "pepmonster", "place": "Gulfport, Mississippi", "lat": 30.46, "lon": -89.0857},
    {"tag": "dannyutxo", "place": "Las Vegas, Nevada", "lat": 36.1674, "lon": -115.1484},
    {"tag": "pumpkin", "place": "Rochester, New York", "lat": 43.2167, "lon": -77.5583},
    {"tag": "dogefeed", "place": "New York, New York", "lat": 40.839, "lon": -73.946},
    {"tag": "strawberry", "place": "Minneapolis, Minnesota", "lat": 44.9777, "lon": -93.265},
    {"tag": "bigchiefenergy", "place": "Minneapolis, Minnesota", "lat": 44.9777, "lon": -93.265},
    {"tag": "banana", "place": "Eastern France", "lat": 48.9742, "lon": 8.1851},
    {"tag": "", "place": "St. Louis, Missouri", "lat": 38.6364, "lon": -90.1985},
)


def page_path():
    """The standalone page. GitHub Pages serves `docs/` at the site root, and a
    checkout of this repo keeps the same file for `/globe`."""
    from pathlib import Path
    here = Path(__file__).resolve()
    for path in (here.parents[1] / "docs" / "nodes-globe.html",
                 here.parents[2] / "docs" / "nodes-globe.html"):
        if path.is_file():
            return path
    return None


def clean_name(value: Any) -> str:
    """An operator tag safe to show, or "". An IP, a hostname, a Cloudflare
    name and a mesh id are not names."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lstrip("@").strip()
    if not text or len(text) > 24 or _IP.search(text) or _IPV6.search(text):
        return ""
    if _HOST.match(text) or "cloudflare" in text.lower() or _HEX.match(text):
        return ""
    text = text.lower()
    return text if _TAG.match(text) else ""


def clean_place(value: Any) -> str:
    """A place label safe to show, or "". Still not coordinates: only a catalog
    match is allowed to be plotted."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > 80 or _IP.search(text) or _IPV6.search(text):
        return ""
    if _HOST.match(text) or "cloudflare" in text.lower():
        return ""
    compact = text.replace(" ", "").replace(",", "")
    if _HEX.match(compact):
        return ""
    return text


def _by_tag() -> dict[str, dict]:
    return {row["tag"]: row for row in PLACES if row["tag"]}


def _by_place() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in PLACES:
        found.setdefault(row["place"].casefold(), row)
    return found


def catalog() -> list[dict]:
    """The places the operator may assign, one row per label."""
    seen: list[dict] = []
    had: set[str] = set()
    for row in PLACES:
        if row["place"] in had:
            continue
        had.add(row["place"])
        seen.append({"place": row["place"], "lat": row["lat"], "lon": row["lon"]})
    return seen


def match_place(value: Any) -> str:
    """The catalog label `value` names, or ""."""
    text = clean_place(value)
    row = _by_place().get(text.casefold()) if text else None
    return row["place"] if row else ""


def _located(label: str) -> dict | None:
    return _by_place().get(label.casefold()) if label else None


def parse_choice(raw: Any) -> dict:
    """The operator's choice, with anything that is not a source, a tag or a
    known place left out. Missing means every arcade on the mesh."""
    said = raw if isinstance(raw, dict) else {}
    source = said.get("source") if said.get("source") in SOURCES else "mesh"
    picked: list[str] = []
    for item in said.get("picked") or []:
        name = clean_name(item)
        if name and name not in picked and len(picked) < 200:
            picked.append(name)
    places: dict[str, str] = {}
    incoming = said.get("places") if isinstance(said.get("places"), dict) else {}
    for tag, place in list(incoming.items())[:200]:
        name = clean_name(tag)
        label = match_place(place)
        if name and label:
            places[name] = label
    return {"source": source, "picked": picked, "places": places}


def bands(chains: int) -> dict:
    """The numeric bands for `chains` (at least one). Gold is open-ended.
    Diamond is everyone tied for the most nodes, not a seat for one person."""
    width = max(1, int(chains))
    return {
        "copper": [1, width],
        "bronze": [width + 1, 2 * width],
        "silver": [2 * width + 1, 3 * width],
        "gold": [3 * width + 1, None],
        "diamond": "most",
    }


def tier_for(nodes: int, chains: int, top: int) -> str:
    """The tier for one member. Inactive is "". Everyone in a band shares it.
    Diamond is the true maximum, shared by anyone tied for it."""
    count = int(nodes)
    if count <= 0:
        return ""
    if int(top) > 0 and count == int(top):
        return "diamond"
    width = max(1, int(chains))
    if count <= width:
        return "copper"
    if count <= 2 * width:
        return "bronze"
    if count <= 3 * width:
        return "silver"
    return "gold"


def _chain(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _CHAIN.match(text) else ""


def _selected(rows: list[dict], choice: dict) -> list[dict]:
    source = choice["source"]
    picked = set(choice["picked"])
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if source == "picked" and clean_name(row.get("tag")) not in picked:
            continue
        if source == "linked" and not (row.get("linked") or row.get("self")):
            continue
        out.append(row)
    return out


def _groups(rows: list[dict], choice: dict) -> tuple[dict[str, dict], set[str]]:
    """Tag -> {nodes, mine, chains}. Chains of every active node, named or not,
    so unnamed arcades still widen the bands without being plotted."""
    groups: dict[str, dict] = {}
    chains: set[str] = set()
    for row in _selected(rows, choice):
        linked = bool(row.get("linked") or row.get("self"))
        row_chains = {_chain(c) for c in (row.get("chains") or [])}
        row_chains.discard("")
        if linked:
            chains |= row_chains
        tag = clean_name(row.get("tag"))
        if not tag:
            continue
        group = groups.setdefault(tag, {"nodes": 0, "mine": False})
        if linked:
            group["nodes"] += 1
        if row.get("self"):
            group["mine"] = True
    if choice["source"] == "picked":
        for tag in choice["picked"]:
            groups.setdefault(tag, {"nodes": 0, "mine": False})
    return groups, chains


def _member(tag: str, group: dict, choice: dict, chains: int, top: int) -> dict:
    label = choice["places"].get(tag) or ""
    if not label:
        known = _by_tag().get(tag)
        label = known["place"] if known else ""
    label = match_place(label)
    located = _located(label)
    nodes = int(group["nodes"])
    member = {
        "tag": tag,
        "place": label,
        "active": nodes > 0,
        "nodes": nodes,
        "tier": tier_for(nodes, chains, top),
        "mine": bool(group["mine"]),
    }
    if located is not None:
        member["lat"] = located["lat"]
        member["lon"] = located["lon"]
    return member


def feed_from(rows: list[dict] | None, choice: Any, now: float | None = None) -> dict:
    """The public document. `rows` may carry hosts and ids; none of that is read."""
    picked = parse_choice(choice)
    groups, chainset = _groups(list(rows or []), picked)
    width = max(1, len(chainset))
    top = max((group["nodes"] for group in groups.values()), default=0)
    members = [_member(tag, group, picked, width, top) for tag, group in groups.items()]
    members.sort(key=lambda row: (-row["nodes"], row["tag"]))
    stamp = hashlib.sha256(json.dumps(
        {"members": members, "chains": width, "source": picked["source"]},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return {
        "ok": True,
        "source": picked["source"],
        "updated": stamp,
        "at": int(now if now is not None else time.time()),
        "chains": width,
        "bands": bands(width),
        "members": members,
    }


def candidates(rows: list[dict] | None, choice: Any) -> list[dict]:
    """Who the operator can tick: a tag, a node count, a place. No mesh id."""
    picked = parse_choice(choice)
    found: dict[str, dict] = {}
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        tag = clean_name(row.get("tag"))
        if not tag:
            continue
        item = found.setdefault(tag, {"tag": tag, "nodes": 0, "active": False})
        if row.get("linked") or row.get("self"):
            item["nodes"] += 1
            item["active"] = True
    for tag in picked["picked"]:
        found.setdefault(tag, {"tag": tag, "nodes": 0, "active": False})
    for tag, item in found.items():
        label = picked["places"].get(tag) or ""
        if not label:
            known = _by_tag().get(tag)
            label = known["place"] if known else ""
        item["place"] = match_place(label)
    return sorted(found.values(), key=lambda row: row["tag"])


def admin_view(rows: list[dict] | None, choice: Any, now: float | None = None) -> dict:
    """What Admin → Overview's Nodes globe panel draws, plus the public feed."""
    picked = parse_choice(choice)
    return {
        **feed_from(rows, picked, now=now),
        "choice": picked,
        "candidates": candidates(rows, picked),
        "places": catalog(),
    }
