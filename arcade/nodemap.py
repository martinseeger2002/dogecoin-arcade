"""Every node this arcade can put on a map.

Two kinds, and both are already public: an arcade that announced an IP on the
messaging chain, and a peer of a chain this machine runs (its Core's
getpeerinfo). A tunnel address has no place, so it is listed and not drawn.
Nothing here is an address the page can copy: the map gets a country and a point.
"""
from __future__ import annotations

import time
from typing import Any

from .geoip import place, public_ip

_CACHE: dict[int, tuple[float, dict]] = {}
EVERY = 30.0


def _addr_host(addr: str) -> str:
    text = str(addr or "").strip()
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def _blank() -> dict:
    return {"names": [], "chains": [], "arcade": False, "me": False, "linked": False, "key": ""}


def collect(state: Any) -> dict:
    """The map's nodes. Cached per process for half a minute: the page asks again on its own."""
    now = time.time()
    kept = _CACHE.get(id(state))
    if kept is not None and now - kept[0] < EVERY:
        return kept[1]
    said = _gather(state)
    _CACHE[id(state)] = (now, said)
    return said


def _gather(state: Any) -> dict:
    found: dict[str, dict] = {}

    def slot(ip: str) -> dict:
        return found.setdefault(ip, _blank())

    mesh = getattr(state, "mesh", None)
    up = mesh is not None and getattr(mesh, "loop", None) is not None
    me_key = (getattr(mesh, "node_id", "") or "") if up else ""
    links = set(dict(mesh.node.links)) if up else set()
    said = dict(getattr(getattr(mesh, "operators", None), "peers", {}) or {}) if up else {}

    own = ""
    stun = getattr(mesh, "stun", None) if mesh is not None else None
    if stun is not None:
        try:
            for host, _port in stun.mine() or []:
                own = public_ip(str(host))
                if own:
                    break
        except Exception:                              # noqa: BLE001 -- drawn without a "you are here"
            own = ""

    try:
        network = state.messaging.network
        with state.store() as store:
            rows = store.mesh_peers(network, since=0)
    except Exception:                                  # noqa: BLE001 -- the chain's announcements, next time
        rows = []
    placed_keys: set[str] = set()

    def _tag(key: str, rec: dict) -> None:
        heard = said.get(key) or {}
        tag = str(heard.get("tag") or "")
        if tag and "@" + tag not in rec["names"]:
            rec["names"].append("@" + tag)
        if key and key == me_key:
            rec["me"] = True
        if key and key in links:
            rec["linked"] = True

    for row in rows:
        ip = public_ip(str(row["host"]))
        if not ip:
            continue
        key = str(row["mesh_key"] or "")
        placed_keys.add(key)
        rec = slot(ip)
        rec["arcade"] = True
        rec["key"] = rec["key"] or key
        _tag(key, rec)
    tunnels: dict[str, dict] = {}
    for row in rows:
        key = str(row["mesh_key"] or "")
        if not key or key in placed_keys or key in tunnels:
            continue
        if not str(row["host"]).startswith("wss://"):
            continue
        rec = _blank()
        rec["arcade"] = True
        rec["key"] = key
        _tag(key, rec)
        tunnels[key] = rec

    if own:
        slot(own)["me"] = True

    local: list[str] = []
    for chain in list(getattr(state, "all_chains", None) or []):
        if getattr(chain, "borrowed", False):
            continue
        label = str(getattr(chain.params, "label", "") or getattr(chain.params, "coin", "") or chain.network)
        if label not in local:
            local.append(label)
        try:
            with chain.rpc() as rpc:
                peers = rpc.call("getpeerinfo") or []
        except Exception:                              # noqa: BLE001 -- a chain with no node is absent
            continue
        for peer in peers:
            ip = public_ip(_addr_host(str(peer.get("addr") or "")))
            if not ip:
                continue
            rec = slot(ip)
            if label not in rec["chains"]:
                rec["chains"].append(label)
    for rec in list(found.values()) + list(tunnels.values()):
        if rec["me"]:
            rec["chains"] = local + [c for c in rec["chains"] if c not in local]

    nodes, unplaced = [], []
    for ip, rec in list(found.items()) + [(k, r) for k, r in tunnels.items()]:
        where = place(ip, salt=rec["key"] or ip)
        name = rec["names"][0] if rec["names"] else ("This arcade" if rec["me"] else "")
        if not name and rec["arcade"]:
            name = "Arcade " + (rec["key"][:8] or "peer")
        if not name:
            name = ", ".join(rec["chains"][:2]) or "Peer"
        row = {"name": name, "me": bool(rec["me"]), "arcade": bool(rec["arcade"]),
               "linked": bool(rec["linked"]), "chains": rec["chains"],
               "country": (where or {}).get("name") or "", "code": (where or {}).get("country") or ""}
        if where and where.get("lat") is not None:
            row.update(lat=where["lat"], lon=where["lon"])
            nodes.append(row)
        else:
            unplaced.append(row)
    nodes.sort(key=lambda r: (not r["me"], not r["arcade"], r["country"], r["name"].lower()))
    unplaced.sort(key=lambda r: (not r["me"], not r["arcade"], r["name"].lower()))
    return {"ok": True, "nodes": nodes, "unplaced": unplaced,
            "counts": {"on_map": len(nodes), "unplaced": len(unplaced),
                       "arcades": sum(1 for r in nodes + unplaced if r["arcade"]),
                       "peers": sum(1 for r in nodes + unplaced if r["chains"])}}
