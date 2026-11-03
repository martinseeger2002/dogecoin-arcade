"""DogeFeedX apps for the Dogecoin Arcade desktop.

Everything these windows show comes from this arcade (2026-11-03: "this is meant not to be reliant on any
external API's or external servers or services. Everything needs to work through arcade"):

    the feed, posts, profiles, trends, the leaderboard   the arcade's own on-chain feed (feed.html's posts)
    collections, items, floors, the market               the collections kept in this arcade's database and its own
                                                         Doginal market's open listings
    inscriptions, DRC-20 tokens, names, memes            this arcade's ord index (images: /content/<id>)
    launchpad                                            the arcade's mintpads
    balances, holdings                                   the watcher's utxo index and the ledger

The data comes from `LOCAL`, the provider app.py hands register_routes (app.py holds the arcade's state). No
endpoint here reaches any other server; tests/test_dogefeedx_local.py keeps it so.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

#: Pictures are this arcade's own content route.
IMG_BASE = "/content"
CACHE_TTL = 30
SOURCE = "arcade"

#: The arcade's data (app.py: dfx_local). None until register_routes: every endpoint answers empty then.
LOCAL = None

_cache: dict[str, tuple[float, Any]] = {}

router = APIRouter(prefix="/dogefeedx", tags=["dogefeedx"])


def _get_cached(cache: dict, key: str, ttl: float = CACHE_TTL) -> Any | None:
    if key in cache:
        ts, value = cache[key]
        if time.time() - ts < ttl:
            return value
    return None


def _set_cached(cache: dict, key: str, value: Any) -> None:
    cache[key] = (time.time(), value)
    if len(cache) > 1000:
        for k, _ in sorted(cache.items(), key=lambda x: x[1][0])[:100]:
            cache.pop(k, None)


async def _local(name: str, *args, empty: dict, ttl: float = CACHE_TTL, **kwargs) -> dict:
    """LOCAL.<name>(...) off the event loop, cached `ttl` seconds; `empty` (with source) when there is no answer."""
    fn = getattr(LOCAL, name, None) if LOCAL is not None else None
    if fn is None:
        return dict(empty, source=SOURCE)
    key = f"{name}:{args!r}:{sorted(kwargs.items())!r}"
    got = _get_cached(_cache, key, ttl) if ttl else None
    if got is not None:
        return got
    try:
        got = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:                                      # noqa: BLE001 -- shown empty, logged
        logger.warning("dogefeedx: %s", name, exc_info=True)
        return dict(empty, source=SOURCE)
    got = dict(got or empty, source=SOURCE, image_base=IMG_BASE)
    if ttl:
        _set_cached(_cache, key, got)
    return got


# --- Static files ---

_STATIC_DIR = Path(__file__).resolve().parent.parent / "templates" / "dogefeedx"


@router.get("/aqua.css")
async def aqua_css():
    """Serve the Aqua theme CSS."""
    path = _STATIC_DIR / "aqua.css"
    if path.is_file():
        return FileResponse(path, media_type="text/css")
    raise HTTPException(status_code=404)


@router.get("/shell.js")
async def shell_js():
    """Serve the shell registration JavaScript."""
    path = _STATIC_DIR / "shell.js"
    if path.is_file():
        return FileResponse(path, media_type="application/javascript")
    raise HTTPException(status_code=404)


EXPLORER_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="eg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#f4d67a"/><stop offset="100%" style="stop-color:#d9a520"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#eg)"/>
<rect x="10" y="14" width="44" height="36" rx="3" fill="#fff" opacity="0.9"/>
<rect x="10" y="14" width="44" height="8" rx="3" fill="#e0e0e0"/>
<circle cx="16" cy="18" r="2" fill="#ff6b6b"/><circle cx="22" cy="18" r="2" fill="#feca57"/>
<circle cx="28" cy="18" r="2" fill="#5cdb5c"/>
<rect x="14" y="26" width="12" height="20" rx="2" fill="#f0f0f0"/>
<rect x="30" y="26" width="8" height="8" rx="2" fill="#d9a520"/>
<rect x="42" y="26" width="8" height="8" rx="2" fill="#e8e8e8"/>
<rect x="30" y="38" width="8" height="8" rx="2" fill="#e8e8e8"/>
<rect x="42" y="38" width="8" height="8" rx="2" fill="#d9a520"/>
</svg>"""

TERMINAL_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="tg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#3a3a3a"/><stop offset="100%" style="stop-color:#1a1a1a"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#tg)"/>
<rect x="8" y="12" width="48" height="40" rx="4" fill="#1a1a1a" stroke="#555" stroke-width="1"/>
<rect x="8" y="12" width="48" height="10" rx="4" fill="#2a2a2a"/>
<circle cx="15" cy="17" r="2.5" fill="#ff6b6b"/><circle cx="22" cy="17" r="2.5" fill="#feca57"/>
<circle cx="29" cy="17" r="2.5" fill="#5cdb5c"/>
<text x="14" y="35" fill="#d9a520" font-family="monospace" font-size="10" font-weight="bold">$</text>
<text x="22" y="35" fill="#33ff33" font-family="monospace" font-size="10">help</text>
<rect x="14" y="40" width="6" height="8" fill="#33ff33" opacity="0.8"/>
</svg>"""

FEED_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="fg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#1a1a1a"/><stop offset="100%" style="stop-color:#000"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#fg)"/>
<circle cx="32" cy="20" r="10" fill="#d9a520"/>
<text x="32" y="24" text-anchor="middle" font-size="12" fill="#000" font-weight="bold">D</text>
<rect x="14" y="34" width="36" height="4" rx="2" fill="#555"/>
<rect x="14" y="42" width="28" height="4" rx="2" fill="#444"/>
<rect x="14" y="50" width="32" height="4" rx="2" fill="#444"/>
<circle cx="50" cy="48" r="6" fill="#d9a520" opacity="0.8"/>
<path d="M48 48 L50 50 L53 46" stroke="#000" stroke-width="1.5" fill="none"/>
</svg>"""

PROFILE_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="pg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#2a2a2a"/><stop offset="100%" style="stop-color:#1a1a1a"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#pg)"/>
<circle cx="32" cy="24" r="12" fill="#d9a520"/>
<path d="M16 54 C16 42 24 36 32 36 C40 36 48 42 48 54" fill="#d9a520"/>
<circle cx="32" cy="24" r="8" fill="#1a1a1a"/>
<text x="32" y="28" text-anchor="middle" font-size="10" fill="#d9a520" font-weight="bold">D</text>
</svg>"""

MARKET_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="mg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#1a1a1a"/><stop offset="100%" style="stop-color:#000"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#mg)"/>
<rect x="10" y="14" width="20" height="20" rx="4" fill="#d9a520"/>
<rect x="34" y="14" width="20" height="20" rx="4" fill="#888"/>
<rect x="10" y="38" width="20" height="20" rx="4" fill="#888"/>
<rect x="34" y="38" width="20" height="20" rx="4" fill="#d9a520"/>
<text x="20" y="28" text-anchor="middle" font-size="10" fill="#000" font-weight="bold">Ð</text>
<text x="44" y="52" text-anchor="middle" font-size="10" fill="#000" font-weight="bold">Ð</text>
</svg>"""

WALLET_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="wg" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0%" style="stop-color:#3a3a3a"/><stop offset="100%" style="stop-color:#2a2a2a"/>
</linearGradient></defs>
<rect width="64" height="64" rx="12" fill="url(#wg)"/>
<rect x="8" y="18" width="48" height="32" rx="6" fill="#d9a520"/>
<rect x="12" y="22" width="40" height="24" rx="4" fill="#1a1a1a"/>
<circle cx="44" cy="34" r="6" fill="#d9a520"/>
<text x="44" y="38" text-anchor="middle" font-size="8" fill="#000" font-weight="bold">Ð</text>
<rect x="12" y="14" width="24" height="8" rx="2" fill="#b8860b"/>
</svg>"""


@router.get("/icon-explorer.png")
async def icon_explorer():
    """Serve the Explorer app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-explorer.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=EXPLORER_ICON_SVG, media_type="image/svg+xml")


@router.get("/icon-terminal.png")
async def icon_terminal():
    """Serve the Terminal app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-terminal.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=TERMINAL_ICON_SVG, media_type="image/svg+xml")


@router.get("/icon-feed.png")
async def icon_feed():
    """Serve the Feed app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-feed.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=FEED_ICON_SVG, media_type="image/svg+xml")


@router.get("/icon-profile.png")
async def icon_profile():
    """Serve the Profile app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-profile.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=PROFILE_ICON_SVG, media_type="image/svg+xml")


@router.get("/icon-market.png")
async def icon_market():
    """Serve the Market app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-market.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=MARKET_ICON_SVG, media_type="image/svg+xml")


@router.get("/icon-wallet.png")
async def icon_wallet():
    """Serve the Wallet app icon (SVG fallback)."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "icon-wallet.png"
    if path.is_file():
        return FileResponse(path, media_type="image/png")
    return Response(content=WALLET_ICON_SVG, media_type="image/svg+xml")


# --- Explorer API ---

_FEED_TABS = {"for-you", "latest", "following", "top", "hot"}


@router.get("/api/feed")
async def api_feed(limit: int = 20, cursor: str = "", tab: str = "for-you"):
    """The arcade's own feed, as DogeFeed's timeline: For you = popular, Latest/Following = newest."""
    limit = min(max(1, limit), 50)
    tab = tab if tab in _FEED_TABS else "for-you"
    cursor = cursor if re.fullmatch(r"[0-9]{0,12}", cursor or "") else ""
    return await _local("feed", tab=tab, limit=limit, cursor=cursor, ttl=10,
                        empty={"items": [], "nextCursor": None, "doginal_holders": []})


@router.get("/api/posts/{post_id}")
async def api_post(post_id: str):
    """One post of the arcade's feed (its txid) and its replies."""
    if not re.fullmatch(r"[0-9a-f]{64}", post_id or ""):
        return {"post": None, "ancestors": [], "replies": [], "source": SOURCE, "error": "not_found"}
    return await _local("post", post_id, ttl=10, empty={"post": None, "ancestors": [], "replies": []})


@router.get("/api/users/{handle}/posts")
async def api_user_posts(handle: str, tab: str = "posts", limit: int = 20, cursor: str = ""):
    """An arcade account's posts (its @name)."""
    handle = (handle or "").lstrip("@")[:40]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", handle):
        return {"handle": handle, "author": None, "card": {}, "items": [], "nextCursor": None, "source": SOURCE}
    tab = tab if tab in ("posts", "replies", "media", "likes") else "posts"
    cursor = cursor if re.fullmatch(r"[0-9]{0,12}", cursor or "") else ""
    return await _local("user_posts", handle, tab=tab, limit=min(max(1, limit), 40), cursor=cursor, ttl=10,
                        empty={"handle": handle, "author": None, "card": {}, "items": [], "nextCursor": None})


@router.get("/api/sidebar")
async def api_sidebar():
    """Trending tags, people and the tipped list, from the arcade's recent posts."""
    return await _local("sidebar", empty={"trending": [], "suggestions": [], "earners": []})


@router.get("/api/leaderboard")
async def api_leaderboard(range: str = "7d"):
    """The arcade's posters by tips and likes. `range` is 1d, 7d, 30d or all."""
    if range not in ("1d", "7d", "30d", "all"):
        range = "7d"
    return await _local("leaderboard", range, empty={"range": range, "earners": []})


@router.get("/api/market/doginals")
async def api_market_doginals():
    """The arcade's collections with their floors on its own Doginal market."""
    return await _local("market", empty={"stats": {}, "collections": []})


@router.get("/api/market/drc20")
async def api_market_drc20(q: str = ""):
    """DRC-20 tokens from this arcade's ord index, in the market board's shape."""
    got = await _local("tokens", limit=200, q=(q or "")[:20], sort="holders", empty={"tokens": []})
    return {"ok": True, "source": SOURCE,
            "tokens": [{"tick": t["ticker"], "holders": t["holder_count"], "pic": "", "floorPrice": None,
                        "listingsDoggy": 0, "minted": t["minted"], "max": t["max_supply"],
                        "progress": t["mint_progress"]} for t in got.get("tokens") or []]}


@router.get("/api/price")
async def api_price():
    """No outside price feed: the arcade does not ask one."""
    return {"ok": False, "price": None, "source": SOURCE}


@router.get("/api/collections")
async def api_collections(offset: int = 0, limit: int = 50, q: str = "", sort: str = "name"):
    """The collections kept in this arcade's database (the imported catalog)."""
    return await _local("collections", offset=max(0, offset), limit=min(max(1, limit), 200), q=(q or "")[:60],
                        sort=sort if sort in ("name", "holders", "recent") else "name",
                        empty={"collections": [], "total": 0, "offset": offset, "limit": limit, "has_more": False})


@router.get("/api/collection/items")
async def api_collection_items(id: str = "", offset: int = 0, limit: int = 50):
    """One collection's items, from this arcade's database."""
    if not id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", id):
        raise HTTPException(status_code=400, detail="Collection ID required")
    return await _local("collection_items", id, offset=max(0, offset), limit=min(max(1, limit), 200),
                        empty={"items": [], "collection_id": id, "offset": offset, "limit": limit, "has_more": False})


@router.get("/api/inscription")
async def api_inscription(id: str = ""):
    """One inscription, from this arcade's ord index."""
    if not re.fullmatch(r"[0-9a-f]{64}i[0-9]{1,6}", id or ""):
        raise HTTPException(status_code=400, detail="Inscription ID required")
    return await _local("inscription", id, empty={"inscription": None})


@router.get("/api/tokens")
async def api_tokens(offset: int = 0, limit: int = 50, q: str = "", sort: str = "holders"):
    """DRC-20 tokens from this arcade's ord index."""
    return await _local("tokens", offset=max(0, offset), limit=min(max(1, limit), 200), q=(q or "")[:20],
                        sort=sort if sort in ("holders", "recent", "progress", "name") else "holders",
                        empty={"tokens": [], "total": 0, "offset": offset, "limit": limit, "has_more": False})


@router.get("/api/token")
async def api_token(ticker: str = ""):
    """One DRC-20 token, from this arcade's ord index."""
    if not ticker or len(ticker) > 20:
        raise HTTPException(status_code=400, detail="Ticker required")
    return await _local("token", ticker, empty={"token": None})


@router.get("/api/names")
async def api_names(offset: int = 0, limit: int = 50, q: str = "", sort: str = "recent"):
    """Names from this arcade's ord index: the one asked for (`q`, e.g. much.doge)."""
    q = (q or "").strip().lower()[:64]
    rows = []
    if q:
        got = await _local("name", q if "." in q else q + ".doge", empty={"found": False})
        if got.get("found"):
            rows = [got["name_info"]]
    return {"names": rows, "total": len(rows), "offset": 0, "limit": limit, "has_more": False, "source": SOURCE}


@router.get("/api/name")
async def api_name(name: str = ""):
    """One name, from this arcade's ord index."""
    name = (name or "").strip().lower()[:64]
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    return await _local("name", name, empty={"found": False, "name": name})


@router.get("/api/search")
async def api_search(q: str = "", type: str = "all"):
    """Posts, people and collections on this arcade."""
    q = (q or "").strip()[:80]
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query too short")
    return await _local("search", q, type if type in ("users", "posts") else "all", ttl=20,
                        empty={"query": q, "users": [], "posts": [], "collections": []}) | {"query": q}


# --- Terminal API ---

@router.get("/api/status")
async def api_status(request: Request):
    """Get node status for the terminal."""
    try:
        from . import state
        
        chains = []
        for c in getattr(state, 'token_chains', []):
            chains.append({
                "name": getattr(c, 'label', str(c)),
                "height": getattr(c, 'height', None),
                "synced": getattr(c, 'synced', False),
            })
        
        return {
            "arcade": getattr(state, 'ARCADE_NAME', 'DogecoinArcade'),
            "version": getattr(state, 'VERSION', 'Unknown'),
            "uptime": "Unknown",
            "connections": len(getattr(state, '_links', [])) if hasattr(state, '_links') else 0,
            "chains": chains,
        }
    except Exception as e:
        logger.warning(f"Error getting status: {e}")
        return {
            "arcade": "DogecoinArcade",
            "version": "Unknown",
            "uptime": "Unknown",
            "connections": 0,
            "chains": [],
        }


@router.get("/api/peers")
async def api_peers(request: Request):
    """List connected peers for the terminal."""
    try:
        from . import state
        
        peers = []
        links = getattr(state, '_links', [])
        for link in links:
            peers.append({
                "id": getattr(link, 'node_id', '') or '',
                "tag": getattr(link, 'tag', '') or '',
                "version": getattr(link, 'version', '') or '',
                "latency": getattr(link, 'latency', None),
            })
        
        return {"peers": peers}
    except Exception as e:
        logger.warning(f"Error getting peers: {e}")
        return {"peers": []}


@router.get("/api/balance")
async def api_balance(address: str = ""):
    """Balance from this node's watcher utxo index. No explorer, no key."""
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    from .wallet_assets import index_owned, reading_of, valid_address

    try:
        valid_address(address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dogecoin address required")
    try:
        owned = await asyncio.to_thread(index_owned, address)
        amount = owned.get("balance_doge") or "0"
        reading = reading_of(owned)
    except Exception:
        amount = "0"
        reading = {"via": "local", "sync": "", "node": "this arcade"}
    return {
        "address": address,
        "balances": [{"chain": "Dogecoin", "amount": str(amount), "symbol": "DOGE"}],
        "source": "node",
        "reading": reading,
    }


# Cache for address inscriptions
_address_inscriptions_cache: dict[str, tuple[float, Any]] = {}


@router.get("/api/address/inscriptions")
async def api_address_inscriptions(
    address: str = "",
    offset: int = 0,
    limit: int = 50
):
    """Doginals the ledger index says this address holds.

    Pictures are this node's `/content/` copies. Collection names may be filled
    later from the public catalog. No explorer, and no outpoint.
    """
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    from .wallet_assets import index_owned, reading_of, valid_address

    try:
        valid_address(address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dogecoin address required")

    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    reading = {"via": "local", "sync": "", "node": "this arcade"}
    try:
        owned = await asyncio.to_thread(index_owned, address)
        pieces = list(owned.get("pieces") or [])
        reading = reading_of(owned)
    except Exception as exc:
        logger.warning(f"Index inscriptions failed: {exc}")
        pieces = []
    page = pieces[offset:offset + limit]
    inscriptions = [{
        "id": piece.get("inscription_id") or "",
        "name": piece.get("title") or "",
        "content_type": "",
        "collection": piece.get("slug") or piece.get("title") or "",
        "image_url": piece.get("image") or "",
    } for piece in page]
    return {
        "address": address,
        "inscriptions": inscriptions,
        "total": len(pieces),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(pieces),
        "source": "node",
        "reading": reading,
    }


@router.get("/api/address/tokens")
async def api_address_tokens(address: str = ""):
    """DRC-20 balances from this arcade's ord index. No explorer, no key."""
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    from .wallet_assets import index_owned, reading_of, valid_address

    try:
        valid_address(address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dogecoin address required")
    reading = {"via": "local", "sync": "", "node": "this arcade"}
    try:
        owned = await asyncio.to_thread(index_owned, address)
        rows = list(owned.get("tokens") or [])
        reading = reading_of(owned)
    except Exception as exc:
        logger.warning(f"Index tokens failed: {exc}")
        rows = []
    tokens = [{
        "ticker": row.get("tick") or "",
        "balance": row.get("balance") or "0",
        "transferable": row.get("balance") or "0",
        "available": row.get("balance") or "0",
    } for row in rows]
    return {"address": address, "tokens": tokens, "source": "node", "reading": reading}


async def _wallets(request: Request) -> dict:
    """Whose addresses the asker's wallet windows read (app.py _dfx_wallets): the operator's watch list, an account's
    own address, or none."""
    fn = getattr(LOCAL, "wallets", None) if LOCAL is not None else None
    if fn is None:
        from .wallet_assets import node_watch_addresses
        return {"operator": True, "addresses": list(await asyncio.to_thread(node_watch_addresses))}
    try:
        return await asyncio.to_thread(fn, request)
    except Exception:                                      # noqa: BLE001 -- nobody's, rather than somebody else's
        return {"operator": False, "addresses": []}


@router.get("/api/wallet/holdings")
async def api_wallet_holdings(request: Request, address: str = ""):
    """Doginals, names, DRC-20, and the balance for the asker's Dogecoin address(es).

    Ownership is this node's indexes. The operator may name any address (an address the index already watches reads
    as imported); an account reads its own address only, never the node's watch list. No key is accepted. Outpoints
    are not returned. Collection labels are the arcade's own.
    """
    from .wallet_assets import empty_holdings, holdings_for, merge_holdings, public_view, valid_address

    said = await _wallets(request)
    watched = list(said.get("addresses") or [])
    if address:
        try:
            valid_address(address)
        except ValueError:
            raise HTTPException(status_code=400, detail="Dogecoin address required")
        if not said.get("operator") and address not in watched:
            return empty_holdings()
        try:
            result = await holdings_for(address, imported=address in set(watched))
        except Exception:
            result = empty_holdings()
        return public_view(result)
    if not watched:
        if not said.get("operator"):
            return dict(empty_holdings(), empty="Sign in to see your wallet." if not said.get("signed_in")
                        else "Your wallet has no Dogecoin address on this arcade yet.")
        return empty_holdings()
    parts = []
    for one in watched[:4]:
        try:
            parts.append(await holdings_for(one, imported=True))
        except Exception:
            continue
    return public_view(merge_holdings(parts) if parts else empty_holdings())


@router.get("/api/wallet/addresses")
async def api_wallet_addresses(request: Request):
    """The asker's Dogecoin addresses: the node's watch list for the operator, an account's own address for it."""
    said = await _wallets(request)
    return {"addresses": list(said.get("addresses") or []), "source": "node" if said.get("operator") else "account"}


@router.get("/api/lookup")
async def api_lookup(name: str = ""):
    """A name from this arcade's name index. The claim's outpoint is not returned."""
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    from .wallet_assets import local_name

    try:
        found = await asyncio.to_thread(local_name, name)
    except Exception:
        found = None
    if not found:
        return {"found": False, "name": name, "source": "node"}
    return {"found": True, "name": found.get("name") or name,
            "inscription": found.get("inscription") or "", "source": "node"}


@router.get("/api/memes")
async def api_memes(q: str = "", offset: int = 0):
    """Pictures inscribed on the chain, from this arcade's ord index."""
    try:
        offset = min(max(0, int(offset)), 10_000_000)
    except (TypeError, ValueError):
        offset = 0
    return await _local("memes", (q or "")[:80], offset, empty={"memes": [], "offset": offset, "more": False})


@router.get("/api/arcade")
async def api_arcade(request: Request):
    """The apps on this arcade, as tiles."""
    return await _local("games", request, ttl=0, empty={"games": []})


@router.get("/api/launchpad")
async def api_launchpad():
    """The arcade's mintpads that are minting."""
    return await _local("launchpad", empty={"launches": []})


@router.get("/api/me")
async def api_me(request: Request):
    """Whoever is asking: @tag, address, and the profile they published on the chain."""
    return await _local("me", request, ttl=0, empty={"signed_in": False, "tag": "", "address": "", "bio": "", "url": ""})


@router.get("/api/whoami")
async def api_whoami(request: Request):
    """Get current user info for the terminal."""
    try:
        from . import state
        account = state._signed_in(request) if hasattr(state, '_signed_in') else None
        if account:
            return {"user": account.tag if hasattr(account, 'tag') else str(account)}
        return {"user": None}
    except Exception:
        return {"user": None}


def _feed_kinds() -> dict:
    """The feed's reaction kinds by name (feed.py BY_NAME), for wallet.react in the browser."""
    try:
        from arcade.messaging import feed as feedlib
        return dict(feedlib.BY_NAME)
    except Exception:                                      # noqa: BLE001
        return {}


def _chain():
    """The arcade's account chain, for a page that signs or seals in the browser (None before register_routes)."""
    try:
        return LOCAL.chain() if LOCAL is not None and hasattr(LOCAL, "chain") else None
    except Exception:                                      # noqa: BLE001
        return None


def register_routes(app, render, local=None):
    """The DogeFeedX pages and API on `app`; `local` is the arcade's data (app.py: dfx_local)."""
    global LOCAL
    LOCAL = local
    app.include_router(router)
    
    @app.get("/dogefeedx/explorer", response_class=HTMLResponse)
    def dogefeedx_explorer(request: Request):
        """The DogeFeed Explorer app - Finder-style inscription browser."""
        return render(request, "dogefeedx/explorer.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/terminal", response_class=HTMLResponse)
    def dogefeedx_terminal(request: Request):
        """The DogeFeed Terminal app - Terminal.app-style command window."""
        return render(request, "dogefeedx/terminal.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/feed", response_class=HTMLResponse)
    def dogefeedx_feed(request: Request):
        """Home: the arcade's own feed in DogeFeed's timeline; Post goes on the chain through the arcade."""
        return render(request, "dogefeedx/feed.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/profile", response_class=HTMLResponse)
    def dogefeedx_profile(request: Request):
        """The DogeFeed profile hub - user profiles with holdings and activity."""
        return render(request, "dogefeedx/profile.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/explore", response_class=HTMLResponse)
    def dogefeedx_explore(request: Request):
        """The DogeFeed explore page - trending, people to follow, collections."""
        return render(request, "dogefeedx/explore.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/market", response_class=HTMLResponse)
    def dogefeedx_market(request: Request):
        """The DogeFeed market - listings sorted cheapest first."""
        return render(request, "dogefeedx/market.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/wallet", response_class=HTMLResponse)
    def dogefeedx_wallet(request: Request):
        """The DogeFeed wallet - read-only view of inscriptions by address."""
        return render(request, "dogefeedx/wallet.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/inscription", response_class=HTMLResponse)
    def dogefeedx_inscription(request: Request):
        """The DogeFeed inscription detail - full content viewer for any inscription."""
        return render(request, "dogefeedx/inscription.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/leaderboard", response_class=HTMLResponse)
    def dogefeedx_leaderboard(request: Request):
        """The DogeFeed leaderboard - top users by activity and holdings."""
        return render(request, "dogefeedx/leaderboard.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/notifications", response_class=HTMLResponse)
    def dogefeedx_notifications(request: Request):
        """The DogeFeed notifications - activity alerts (requires sign-in)."""
        return render(request, "dogefeedx/notifications.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/messages", response_class=HTMLResponse)
    def dogefeedx_messages(request: Request):
        """The DogeFeed messages - direct messages (requires sign-in)."""
        return render(request, "dogefeedx/messages.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/bookmarks", response_class=HTMLResponse)
    def dogefeedx_bookmarks(request: Request):
        """The DogeFeed bookmarks - saved posts (requires sign-in)."""
        return render(request, "dogefeedx/bookmarks.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/collections", response_class=HTMLResponse)
    def dogefeedx_collections(request: Request):
        """The DogeFeed collections - browse all Doginal collections."""
        return render(request, "dogefeedx/collections.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/nfts", response_class=HTMLResponse)
    def dogefeedx_nfts(request: Request):
        """The DogeFeed NFTs page - browse and discover NFTs."""
        return render(request, "dogefeedx/nfts.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/launchpad", response_class=HTMLResponse)
    def dogefeedx_launchpad(request: Request):
        """The DogeFeed launchpad - new project launches and mints."""
        return render(request, "dogefeedx/launchpad.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/search", response_class=HTMLResponse)
    def dogefeedx_search(request: Request):
        """The DogeFeed search page - global search results."""
        return render(request, "dogefeedx/search.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
    
    @app.get("/dogefeedx/post", response_class=HTMLResponse)
    def dogefeedx_post(request: Request):
        """The DogeFeed post detail - individual post view with replies."""
        return render(request, "dogefeedx/post.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())

    @app.get("/dogefeedx/files", response_class=HTMLResponse)
    def dogefeedx_files(request: Request):
        """Finder-style view of this arcade wallet: collections, DRC-20 and names."""
        return render(request, "dogefeedx/files.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())

    @app.get("/dogefeedx/inscribe", response_class=HTMLResponse)
    def dogefeedx_inscribe(request: Request):
        """Inscribe a collection, a DRC-20 or a name with this arcade's wallet."""
        return render(request, "dogefeedx/inscribe.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())

    @app.get("/dogefeedx/arcade", response_class=HTMLResponse)
    def dogefeedx_arcade(request: Request):
        """Games on this arcade. The player is the on-chain account."""
        return render(request, "dogefeedx/arcade.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())

    @app.get("/dogefeedx/memes", response_class=HTMLResponse)
    def dogefeedx_memes(request: Request):
        """Pictures on the chain, from this arcade's ord index."""
        return render(request, "dogefeedx/memes.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())

    @app.get("/dogefeedx/settings", response_class=HTMLResponse)
    def dogefeedx_settings(request: Request):
        """Settings kept on this arcade."""
        return render(request, "dogefeedx/settings.html", fullscreen=True, chain=_chain(), feed_kinds=_feed_kinds())
