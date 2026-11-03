"""Watch-only holdings for one Dogecoin address.

What the address owns comes from this node: the watcher's utxo index (the
balance), the ledger's inscription index (doginals), and the ord index
(DRC-20 balances and names). dogefeed.app is read only to label a collection
this index already holds (its public name, cover, and floor). Nothing here
asks for a key, signs, or spends, and nothing is written to dogefeed.app.

Outpoints are asked of the name index inside this process and are not returned.
"""
from __future__ import annotations

import re
import threading
from html import unescape

import httpx

from .public_read import parse_market

DOGEFEED_ORIGIN = "https://dogefeed.app"
_HTML_UA = {"User-Agent": "DogecoinArcade-DogeFeedX-readonly", "Accept": "text/html"}

# A mainnet Dogecoin address. No bech32, no key material, no testnet n-address.
_ADDRESS = re.compile(r"^D[1-9A-HJ-NP-Za-km-z]{25,33}$")
_INSCRIPTION = re.compile(r"^[0-9a-f]{64}i\d{1,6}$")
_SLUG = re.compile(r"^[a-z0-9-]{1,80}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOGE = re.compile(r"^\d+(?:\.\d+)?$")
_TICK = re.compile(r"^\$?[A-Za-z0-9]{1,12}$")
_CARD = re.compile(
    r'data-inscription="([0-9a-f]{64}i\d{1,6})"[\s\S]{0,700}?'
    r'href="(/market/([a-z0-9-]+)/(\d+))"[\s\S]{0,500}?'
    r'<img src="([^"]+)" alt="([^"]*)"'
)
_EMPTY = "Import a watch-only address on this node."

# Spend-shaped keys that must never leave this module, even if a caller passes a wallet row.
_SPEND_KEYS = {
    "scriptPubKey", "scriptPubKeyHex", "script", "txid", "vout", "value",
    "value_koinu", "hex", "witness", "private_key", "privkey", "wif",
}


def valid_address(address: str) -> str:
    text = (address or "").strip()
    if not _ADDRESS.fullmatch(text):
        raise ValueError("not a Dogecoin address")
    return text


def inscriptions_from_utxos(rows) -> list[dict]:
    """Inscription id and type only. Drops scripts, outpoints, and amounts."""
    found: list[dict] = []
    seen: set[str] = set()
    if not isinstance(rows, list):
        return found
    for row in rows:
        nested = row.get("inscriptions") if isinstance(row, dict) else None
        if isinstance(nested, dict):
            nested = [nested]
        for ins in nested or []:
            if not isinstance(ins, dict):
                continue
            iid = str(ins.get("id") or ins.get("inscription_id") or "")
            if not _INSCRIPTION.fullmatch(iid) or iid in seen:
                continue
            seen.add(iid)
            mime = str(ins.get("content_type") or ins.get("mime_type") or ins.get("mime") or "")[:80]
            found.append({"inscription_id": iid, "content_type": mime})
    return found


def parse_names(payload, address: str) -> list[dict]:
    """Names in a payload whose `address` is this one. Sold names (`held` false) are dropped."""
    if not isinstance(payload, dict):
        return []
    said = str(payload.get("address") or "")
    if said and said != address:
        return []
    out = []
    seen: set[str] = set()
    for row in payload.get("names") or []:
        if not isinstance(row, dict) or row.get("held") is False:
            continue
        name = str(row.get("name") or "")
        if not _NAME.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "address": address,
            "held": True,
            "verified": bool(row.get("verified")),
        })
    return out


def name_points_at(payload, address: str, dns: str) -> bool:
    """dogefeed.app's public name record resolves `dns` to this address."""
    if not isinstance(payload, dict):
        return False
    for row in payload.get("records") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("dns") or "") == dns and str(row.get("address") or "") == address:
            return True
    return False


def parse_balance(payload) -> str | None:
    """The public `balance_doge` string. The legacy `balance` field is not DOGE."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("balance_doge")
    text = "" if raw is None else str(raw).strip()
    if _DOGE.fullmatch(text):
        return text
    return None


def parse_drc20(payload) -> list[dict]:
    rows = payload.get("balances") if isinstance(payload, dict) else None
    out = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("tick") or row.get("token") or "")
        balance = str(row.get("balance") or "").split(".", 1)[0]
        if not _TICK.fullmatch(tick) or not _DOGE.fullmatch(balance) or tick in seen:
            continue
        seen.add(tick)
        out.append({"tick": tick, "balance": balance})
    return out


def _safe_image(src: str) -> str:
    text = unescape(src or "").strip()
    if re.fullmatch(r"/content/[0-9a-fA-F]{64}", text):
        return text
    if text.startswith("/doginal-img/") or text.startswith("/api/media/"):
        return DOGEFEED_ORIGIN + text
    if text.startswith("https://dogefeed.app/") or text.startswith("https://cdn.doggy.market/"):
        return text
    return ""


def _koinu_doge(koinu: int) -> str:
    whole, frac = divmod(int(koinu), 100_000_000)
    text = f"{whole}.{frac:08d}".rstrip("0").rstrip(".")
    return text or "0"


def _as_slug(name: str) -> str:
    slug = (name or "").strip().lower()
    return slug if _SLUG.fullmatch(slug) else ""


def _doge_chain():
    """The Dogecoin chain this process already runs, or None."""
    from arcade.web import state

    chains = []
    for attr in ("ledger", "messaging"):
        ctx = getattr(state, attr, None)
        if ctx is not None:
            chains.append(ctx)
    chains.extend(list(getattr(state, "addons", None) or []))
    chains.extend(list(getattr(state, "all_chains", None) or []))
    for ctx in chains:
        if getattr(ctx, "is_doge", False):
            return ctx
    return None


def parse_profile_assets(html: str) -> list[dict]:
    """Doginals on a profile's public assets grid: inscription, collection slug, edition.

    Only the first `profile-nfts` grid. Later copies of the page payload repeat
    the whole market and are not what this profile holds.
    """
    if not html:
        return []
    start = html.find('data-testid="profile-nfts"')
    if start < 0:
        start = html.find('data-testid="doginal-grid"')
    if start < 0:
        return []
    end = html.find("Show more", start)
    chunk = html[start:end if end > start else start + 2_000_000]
    found = []
    seen: set[str] = set()
    for iid, href, slug, edition, src, alt in _CARD.findall(chunk):
        if iid in seen or not _SLUG.fullmatch(slug):
            continue
        seen.add(iid)
        title = unescape(alt).strip()
        found.append({
            "inscription_id": iid,
            "slug": slug,
            "edition": edition,
            "href": DOGEFEED_ORIGIN + href,
            "image": _safe_image(src),
            "title": title,
        })
    return found


def _catalog_maps(catalog: list[dict]) -> tuple[dict, dict]:
    by_slug: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in catalog or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").lower()
        if _SLUG.fullmatch(slug):
            by_slug[slug] = row
        name = str(row.get("name") or "").strip().lower()
        if name:
            by_name[name] = row
    return by_slug, by_name


def cross_reference(held: list[dict], catalog: list[dict]) -> list[dict]:
    """Group held doginals by collection and fill name, cover, and floor from the catalog.

    A held row needs `slug` (or a collection name the catalog uses). Pieces keep
    the edition and inscription id. A slug the catalog does not know stays in
    the list with `known` false and the title the holding already carried.
    """
    by_slug, by_name = _catalog_maps(catalog)
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in held or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").lower()
        title = str(item.get("title") or item.get("name") or "")
        label = title.split(" #", 1)[0].strip() if " #" in title else title.strip()
        cat = by_slug.get(slug) or by_name.get(label.lower()) or by_name.get(slug)
        if not slug and cat:
            slug = str(cat.get("slug") or "")
        key = slug or label or "unlisted"
        if key not in groups:
            image = ""
            floor = None
            name = label or slug or "Unlisted"
            known = cat is not None
            if cat:
                name = str(cat.get("name") or name)
                image = _safe_image(str(cat.get("image") or ""))
                floor = cat.get("floor")
            groups[key] = {
                "slug": slug,
                "name": name,
                "cover": image,
                "floor": floor,
                "href": (DOGEFEED_ORIGIN + f"/market/{slug}") if slug else "",
                "count": 0,
                "known": known,
                "pieces": [],
            }
            order.append(key)
        group = groups[key]
        try:
            stated = int(item.get("group_count") or 0)
        except (TypeError, ValueError):
            stated = 0
        if stated > group["count"]:
            group["count"] = stated
        elif not stated:
            group["count"] += 1
        piece = {
            "edition": str(item.get("edition") or ""),
            "inscription_id": str(item.get("inscription_id") or ""),
            "title": title,
            "image": _safe_image(str(item.get("image") or "")) or group["cover"],
            "href": str(item.get("href") or ""),
        }
        if piece["inscription_id"] and not _INSCRIPTION.fullmatch(piece["inscription_id"]):
            piece["inscription_id"] = ""
        if piece["href"] and not piece["href"].startswith(DOGEFEED_ORIGIN + "/market/"):
            piece["href"] = ""
        group["pieces"].append(piece)
    return [groups[key] for key in order]


def _clean_names(rows) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not _NAME.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        out.append({"name": name})
    return out


def _clean_tokens(rows) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("tick") or "")
        balance = str(row.get("balance") or "")
        if not _TICK.fullmatch(tick) or not _DOGE.fullmatch(balance) or tick in seen:
            continue
        seen.add(tick)
        out.append({"tick": tick, "balance": balance})
    return out


def _units_text(units, decimals: int) -> str:
    text = str(units or "0").split(".", 1)[0]
    if not text.isdigit():
        return ""
    decimals = max(0, int(decimals or 0))
    if decimals:
        text = text.zfill(decimals + 1)
        cut = len(text) - decimals
        text = (text[:cut] + "." + text[cut:]).rstrip("0").rstrip(".")
    return text


def _tokens_from_index(rows) -> list[dict]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        shown = _units_text(row.get("units"), int(row.get("decimals") or 0))
        if not shown:
            continue
        out.append({"tick": str(row.get("tick") or ""), "balance": shown})
    return _clean_tokens(out)


def _names_from_index(rows) -> list[dict]:
    return _clean_names([{"name": row.get("name")} for row in rows or [] if isinstance(row, dict)])


def _pieces_from_index(index, address: str) -> list[dict]:
    """Doginals the ledger says this address holds. Pictures are this node's /content/."""
    found: list[dict] = []
    try:
        groups = index.held_collections(address, cover=1)
    except Exception:
        return found
    for group in groups or []:
        collection = str(group.get("collection") or "")
        try:
            total = int(group.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            rows, _total = index.held_pieces(address, collection, limit=24)
        except Exception:
            rows = []
        if not rows:
            if total:
                found.append({
                    "slug": _as_slug(collection),
                    "title": collection or "Unlisted",
                    "edition": "",
                    "inscription_id": "",
                    "image": "",
                    "group_count": total,
                })
            continue
        for row in rows:
            txid = str(row.get("txid") or "")
            edition = row.get("edition")
            title = str(row.get("name") or collection or "")
            image = f"/content/{txid}" if re.fullmatch(r"[0-9a-fA-F]{64}", txid) else ""
            found.append({
                "slug": _as_slug(collection),
                "title": title or collection or "Unlisted",
                "edition": "" if edition is None else str(edition),
                "inscription_id": txid if _INSCRIPTION.fullmatch(txid) else "",
                "image": image,
                "group_count": total or len(rows),
            })
    return found


def index_owned(address: str) -> dict:
    """Balance, doginals, DRC-20, and names from this node's indexes.

    The watcher utxo rows are summed for the balance and handed to the name
    index. They are not part of the result: no script, no outpoint.
    """
    address = valid_address(address)
    out: dict = {"balance_doge": None, "names": [], "tokens": [], "pieces": []}
    chain = _doge_chain()
    if chain is None:
        return out
    from arcade.web import state
    from arcade import utxos as utxoslib
    import contextlib

    coins: list[list] = []
    try:
        index = state.token_index(chain)
        with contextlib.closing(index.open()) as db:
            out["balance_doge"] = _koinu_doge(utxoslib.balance(db, address))
            for coin in utxoslib.unspent(db, address)[:3000]:
                txid = str(coin.get("txid") or "")
                if re.fullmatch(r"[0-9a-fA-F]{64}", txid):
                    coins.append([txid, int(coin.get("vout") or 0)])
        out["pieces"] = _pieces_from_index(index, address)
    except Exception:
        index = None
    try:
        ordix = state.ord_index(getattr(chain, "network", "") or "doge", wait=0.8)
    except Exception:
        ordix = None
    if ordix is not None:
        try:
            out["tokens"] = _tokens_from_index(ordix.balances(address))
        except Exception:
            pass
        if coins:
            try:
                out["names"] = _names_from_index(ordix.names_on(coins))
            except Exception:
                pass
    return out


def local_name(name: str) -> dict | None:
    """One name from this arcade's name index. The claim's outpoint is dropped."""
    chain = _doge_chain()
    if chain is None:
        return None
    from arcade.web import state

    try:
        ordix = state.ord_index(getattr(chain, "network", "") or "doge", wait=0.8)
    except Exception:
        return None
    if ordix is None:
        return None
    try:
        said = ordix.name(name)
    except Exception:
        return None
    wanted = str((said or {}).get("name") or name).strip().lower()
    for reading in ((said or {}).get("readings") or {}).values():
        winner = (reading or {}).get("winner") or {}
        iid = str(winner.get("inscription") or "")
        if _INSCRIPTION.fullmatch(iid):
            return {"name": wanted, "inscription": iid}
    return None


def build_holdings(
    address: str,
    owned: dict,
    catalog: list[dict],
    *,
    imported: bool,
) -> dict:
    """Label index holdings with the public collection catalog. Ownership is already decided."""
    address = valid_address(address)
    owned = owned if isinstance(owned, dict) else {}
    balance = owned.get("balance_doge")
    if balance is not None and not _DOGE.fullmatch(str(balance)):
        balance = None
    names = _clean_names(owned.get("names"))
    tokens = _clean_tokens(owned.get("tokens"))
    collections = cross_reference(list(owned.get("pieces") or []), catalog)
    profile = next((row["name"] for row in names if row["name"].endswith(".dogefeed")), "")
    empty = ""
    if not imported and not names and not collections and not tokens and balance is None:
        empty = _EMPTY
    elif imported and balance is None and not names and not collections and not tokens:
        empty = "This node's index has no holdings for that address yet."
    return {
        "read_only": True,
        "imported": bool(imported),
        "addresses": [address],
        "balance_doge": balance,
        "names": names,
        "tokens": tokens,
        "collections": collections,
        "profile": profile,
        "empty": empty,
        "source": "node",
    }


def merge_holdings(parts: list[dict]) -> dict:
    """Several watched addresses, one window. Pieces stay deduped by inscription id."""
    parts = [part for part in parts if isinstance(part, dict) and part.get("addresses")]
    if not parts:
        return empty_holdings()
    if len(parts) == 1:
        first = dict(parts[0])
        first["imported"] = True
        first["empty"] = "" if first.get("names") or first.get("collections") else first.get("empty") or ""
        return first
    addresses: list[str] = []
    names: list[dict] = []
    tokens: list[dict] = []
    seen_names: set[str] = set()
    seen_ticks: set[str] = set()
    seen_ins: set[str] = set()
    held: list[dict] = []
    balance = None
    for part in parts:
        for addr in part.get("addresses") or []:
            if addr not in addresses:
                addresses.append(addr)
        if balance is None and part.get("balance_doge"):
            balance = part["balance_doge"]
        for row in part.get("names") or []:
            if row.get("name") and row["name"] not in seen_names:
                seen_names.add(row["name"])
                names.append(row)
        for row in part.get("tokens") or []:
            if row.get("tick") and row["tick"] not in seen_ticks:
                seen_ticks.add(row["tick"])
                tokens.append(row)
        for group in part.get("collections") or []:
            for piece in group.get("pieces") or []:
                iid = piece.get("inscription_id") or ""
                if iid and iid in seen_ins:
                    continue
                if iid:
                    seen_ins.add(iid)
                held.append({
                    "slug": group.get("slug") or "",
                    "edition": piece.get("edition") or "",
                    "inscription_id": iid,
                    "title": piece.get("title") or "",
                    "image": piece.get("image") or "",
                    "href": piece.get("href") or "",
                })
    collections = cross_reference(held, [
        {"slug": group.get("slug"), "name": group.get("name"), "image": group.get("cover"),
         "floor": group.get("floor")}
        for part in parts for group in (part.get("collections") or [])
    ])
    profile = next((row["name"] for row in names if str(row.get("name") or "").endswith(".dogefeed")), "")
    return {
        "read_only": True,
        "imported": True,
        "addresses": addresses,
        "balance_doge": balance,
        "names": names,
        "tokens": tokens,
        "collections": collections,
        "profile": profile,
        "empty": "" if names or collections or tokens or balance else "This node's index has no holdings for that wallet yet.",
    }


def empty_holdings() -> dict:
    return {
        "read_only": True,
        "imported": False,
        "addresses": [],
        "balance_doge": None,
        "names": [],
        "tokens": [],
        "collections": [],
        "profile": "",
        "empty": _EMPTY,
    }


def _node_watch_addresses() -> list[str]:
    """Address strings the Dogecoin wallet already watches. No coins, no scripts."""
    from arcade.web import state

    found: list[str] = []
    contexts = [getattr(state, "messaging", None), getattr(state, "ledger", None)]
    contexts += list(getattr(state, "addons", None) or [])
    for ctx in contexts:
        if ctx is None or not getattr(ctx, "is_doge", False):
            continue
        try:
            with ctx.rpc() as rpc:
                rows = rpc.call("listreceivedbyaddress", 0, True, True) or []
        except Exception:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            addr = str(row.get("address") or "")
            if _ADDRESS.fullmatch(addr) and addr not in found:
                found.append(addr)
    return found


def node_watch_addresses() -> list[str]:
    """The node's watch-only Dogecoin addresses, or nothing if the node does not answer quickly."""
    box: list[list[str]] = []

    def work() -> None:
        try:
            box.append(_node_watch_addresses())
        except Exception:
            box.append([])

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(2.0)
    return list(box[0]) if box else []


_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float):
    import time
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store(key: str, value, ttl: float) -> None:
    import time
    _cache[key] = (time.time() + ttl, value)


async def _get_html(url: str, timeout: float) -> str:
    hit = _cached(url, 600)
    if isinstance(hit, str):
        return hit
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers=_HTML_UA)
            response.raise_for_status()
            text = response.text
    except Exception:
        return ""
    _store(url, text, 600)
    return text


async def catalog_collections() -> list[dict]:
    hit = _cached("market-catalog", 600)
    if isinstance(hit, list):
        return hit
    html = await _get_html(DOGEFEED_ORIGIN + "/market", 20)
    rows = parse_market(html).get("collections") if html else []
    rows = rows if isinstance(rows, list) else []
    _store("market-catalog", rows, 600)
    return rows


async def holdings_for(address: str, *, imported: bool) -> dict:
    """Index holdings for one address, labeled from the public collection catalog.

    `imported` is true only when the address is already in the node wallet.
    The catalog names collections. It does not decide what the address owns.
    """
    import asyncio

    address = valid_address(address)
    owned = await asyncio.to_thread(index_owned, address)
    catalog = await catalog_collections()
    return build_holdings(address, owned, catalog, imported=imported)


def public_view(result: dict) -> dict:
    """Drop anything that is not a display field. Defense in case a caller stuffed a wallet row in."""
    if not isinstance(result, dict):
        return empty_holdings()
    cleaned = {key: value for key, value in result.items() if key not in _SPEND_KEYS}
    return cleaned
