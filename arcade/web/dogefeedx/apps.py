"""DogeFeedX: DogeFeed-branded Mac OS X-style apps for the Dogecoin Arcade.

Two self-contained apps that register with the shell's dock:
1. Explorer: A Finder-style browser of inscription collections from DogeFeed
2. Terminal: A Terminal.app-style window for inline commands

Data sources:
- dogefeed.app: Social feed (/api/feed), search (/api/search), doginal images (/doginal-img/)
- explorer.dogesoft.io: Collection metadata fallback (DogeFeed does not expose a public
  collection listing API; their collection pages are server-side rendered)

All results are cached locally with graceful fallback when services are unreachable.
"""
from __future__ import annotations

import asyncio
import time
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

DOGEFEED_APP_BASE = "https://dogefeed.app"
DOGEFEED_EXPLORER_BASE = "https://explorer.dogesoft.io/api"
DOGEFEED_IMG_BASE = "https://dogefeed.app/doginal-img"
CACHE_TTL = 300  # 5 minutes
REQUEST_TIMEOUT = 10.0

_collection_cache: dict[str, tuple[float, Any]] = {}
_collection_items_cache: dict[str, tuple[float, Any]] = {}

router = APIRouter(prefix="/dogefeedx", tags=["dogefeedx"])


def _get_cached(cache: dict, key: str, ttl: float = CACHE_TTL) -> Any | None:
    """Get a cached value if it exists and hasn't expired."""
    if key in cache:
        ts, value = cache[key]
        if time.time() - ts < ttl:
            return value
    return None


def _set_cached(cache: dict, key: str, value: Any) -> None:
    """Set a cached value with current timestamp."""
    cache[key] = (time.time(), value)
    if len(cache) > 1000:
        oldest = sorted(cache.items(), key=lambda x: x[1][0])[:100]
        for k, _ in oldest:
            cache.pop(k, None)


async def _fetch_dogefeed_app(path: str, params: dict | None = None) -> dict:
    """Fetch data from dogefeed.app API with timeout and error handling.
    
    dogefeed.app provides: /api/feed, /api/search, /doginal-img/{id}
    """
    url = f"{DOGEFEED_APP_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        logger.warning(f"DogeFeed.app API timeout: {path}")
        raise HTTPException(status_code=504, detail="DogeFeed API timeout")
    except httpx.HTTPStatusError as e:
        logger.warning(f"DogeFeed.app API error: {e.response.status_code}")
        raise HTTPException(status_code=502, detail=f"DogeFeed API error: {e.response.status_code}")
    except Exception as e:
        logger.warning(f"DogeFeed.app API error: {e}")
        raise HTTPException(status_code=502, detail="Unable to reach DogeFeed")


async def _fetch_dogefeed_explorer(path: str, params: dict | None = None) -> dict:
    """Fetch collection data from explorer.dogesoft.io (DogeFeed's explorer).
    
    Used for structured collection listings since dogefeed.app doesn't expose a
    public collection API (their collection pages are server-side rendered).
    """
    url = f"{DOGEFEED_EXPLORER_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        logger.warning(f"DogeFeed Explorer API timeout: {path}")
        raise HTTPException(status_code=504, detail="DogeFeed Explorer API timeout")
    except httpx.HTTPStatusError as e:
        logger.warning(f"DogeFeed Explorer API error: {e.response.status_code}")
        raise HTTPException(status_code=502, detail=f"DogeFeed Explorer API error: {e.response.status_code}")
    except Exception as e:
        logger.warning(f"DogeFeed Explorer API error: {e}")
        raise HTTPException(status_code=502, detail="Unable to reach DogeFeed Explorer")


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
async def api_feed(
    limit: int = 20,
    cursor: str = "",
    tab: str = "for-you",
):
    """Get the DogeFeed social feed from dogefeed.app.
    
    Returns posts from verified doginal holders and recent activity.
    This is the primary dogefeed.app data source.
    """
    limit = min(max(1, limit), 50)
    tab = tab if tab in _FEED_TABS else "for-you"
    
    cache_key = f"feed:{tab}:{cursor}:{limit}"
    cached = _get_cached(_collection_cache, cache_key, ttl=60)  # 1 min cache for feed
    if cached is not None:
        return cached
    
    try:
        params = {"tab": tab, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await _fetch_dogefeed_app("/api/feed", params)
        
        items = data.get("items", []) if isinstance(data, dict) else data
        
        doginal_holders = []
        seen_holders = set()
        for item in items:
            author = item.get("author", {})
            if author.get("verifiedPfp") and author.get("id") not in seen_holders:
                avatar = author.get("avatarUrl", "")
                inscription_id = ""
                if avatar.startswith("/doginal-img/"):
                    inscription_id = avatar.replace("/doginal-img/", "")
                doginal_holders.append({
                    "handle": author.get("handle", ""),
                    "display_name": author.get("displayName", ""),
                    "pfp_rarity": author.get("pfpRarity"),
                    "inscription_id": inscription_id,
                    "avatar_url": f"{DOGEFEED_APP_BASE}{avatar}" if avatar.startswith("/") else avatar,
                })
                seen_holders.add(author.get("id"))
        
        result = {
            "items": items[:limit],
            "doginal_holders": doginal_holders,
            "source": "dogefeed.app",
            "image_base": DOGEFEED_IMG_BASE,
        }
        
        _set_cached(_collection_cache, cache_key, result)
        return result
        
    except HTTPException:
        return {"items": [], "doginal_holders": [], "source": "dogefeed.app", "error": "unreachable"}
    except Exception as e:
        logger.warning(f"Error fetching feed: {e}")
        return {"items": [], "doginal_holders": [], "source": "dogefeed.app", "error": str(e)}


@router.get("/api/posts/{post_id}")
async def api_post(post_id: str):
    """One public post, its ancestors and its replies. Empty on failure."""
    from .public_read import PublicReadError, empty, fetch_json, valid_post_id

    try:
        post_id = valid_post_id(post_id)
        data = await fetch_json(f"/api/posts/{post_id}")
        data["source"] = "dogefeed.app"
        return data
    except (PublicReadError, HTTPException) as e:
        return empty(str(getattr(e, "detail", e) or e), post=None, replies=[])
    except Exception as e:
        logger.warning(f"Error fetching post: {e}")
        return empty("unreachable", post=None, replies=[])


@router.get("/api/users/{handle}/posts")
async def api_user_posts(handle: str, tab: str = "posts", limit: int = 20):
    """A public profile's posts, plus follower counts from the public profile page."""
    from .public_read import (
        PublicReadError, empty, fetch_html, fetch_json, parse_profile_card, valid_handle,
    )

    try:
        handle = valid_handle(handle)
        tab = tab if tab in ("posts", "replies", "media", "likes") else "posts"
        limit = min(max(1, limit), 40)
        data = await fetch_json(
            f"/api/users/{handle}/posts",
            {"tab": tab, "limit": limit},
        )
        card = {}
        try:
            card = parse_profile_card(await fetch_html(f"/u/{handle}"), handle)
        except Exception:
            card = {}
        author = None
        for item in data.get("items") or []:
            if item.get("author"):
                author = item["author"]
                break
        return {
            "handle": handle,
            "author": author,
            "card": card,
            "items": data.get("items") or [],
            "nextCursor": data.get("nextCursor"),
            "source": "dogefeed.app",
        }
    except (PublicReadError, HTTPException) as e:
        return empty(str(getattr(e, "detail", e) or e), handle=handle, author=None, card={}, items=[])
    except Exception as e:
        logger.warning(f"Error fetching profile: {e}")
        return empty("unreachable", handle=handle, author=None, card={}, items=[])


@router.get("/api/sidebar")
async def api_sidebar():
    """Trending tags, who to follow, and the 7-day earner list from the public home page."""
    from .public_read import empty, fetch_html, parse_leaderboard, parse_suggestions, parse_trending

    try:
        html = await fetch_html("/")
        return {
            "trending": parse_trending(html),
            "suggestions": parse_suggestions(html),
            "earners": parse_leaderboard(html),
            "source": "dogefeed.app",
        }
    except Exception as e:
        logger.warning(f"Error fetching sidebar: {e}")
        return empty("unreachable", trending=[], suggestions=[], earners=[])


@router.get("/api/leaderboard")
async def api_leaderboard(range: str = "7d"):
    """Public earner board. `range` is 1d, 7d, 30d or all. Empty on failure."""
    from .public_read import empty, fetch_html, parse_leaderboard_page

    if range not in ("1d", "7d", "30d", "all"):
        range = "7d"
    try:
        html = await fetch_html("/leaderboard", {"range": range})
        return {
            "range": range,
            "earners": parse_leaderboard_page(html),
            "source": "dogefeed.app",
        }
    except Exception as e:
        logger.warning(f"Error fetching leaderboard: {e}")
        return empty("unreachable", range=range, earners=[])


@router.get("/api/market/doginals")
async def api_market_doginals():
    """Doginal collections as dogefeed.app renders them on /market."""
    from .public_read import empty, fetch_html, parse_market

    try:
        return parse_market(await fetch_html("/market"))
    except Exception as e:
        logger.warning(f"Error fetching market: {e}")
        return empty("unreachable", stats={}, collections=[])


@router.get("/api/market/drc20")
async def api_market_drc20(q: str = ""):
    """Public DRC-20 market board from dogefeed.app."""
    from .public_read import empty, fetch_json

    try:
        params = {"q": q} if q else None
        data = await fetch_json("/api/drc20/market", params)
        data["source"] = "dogefeed.app"
        return data
    except Exception as e:
        logger.warning(f"Error fetching DRC-20 market: {e}")
        return empty("unreachable", ok=False, tokens=[])


@router.get("/api/price")
async def api_price():
    """Public DOGE/USD price dogefeed.app publishes. Empty on failure."""
    from .public_read import empty, fetch_json

    try:
        data = await fetch_json("/api/price")
        data["source"] = "dogefeed.app"
        return data
    except Exception as e:
        logger.warning(f"Error fetching price: {e}")
        return empty("unreachable", ok=False, price=None)


async def _fetch_collection_sample_items(collection_id: str, count: int = 3) -> list[str]:
    """Fetch a few sample item IDs from a collection for use as icons.
    
    Returns a list of inscription IDs (up to `count`) that can be used as
    representative images for the collection icon.
    """
    cache_key = f"sample_items:{collection_id}:{count}"
    cached = _get_cached(_collection_items_cache, cache_key, ttl=600)  # 10 min cache
    if cached is not None:
        return cached
    
    try:
        data = await _fetch_dogefeed_explorer(
            f"/explorer/collections/{collection_id}/items",
            {"offset": 0, "limit": count}
        )
        items_raw = data if isinstance(data, list) else data.get("items", data.get("inscriptions", []))
        item_ids = [
            i.get("id") or i.get("inscription_id") or ""
            for i in items_raw[:count]
            if i.get("id") or i.get("inscription_id")
        ]
        _set_cached(_collection_items_cache, cache_key, item_ids)
        return item_ids
    except Exception:
        return []


@router.get("/api/collections")
async def api_collections(
    offset: int = 0,
    limit: int = 50,
    q: str = "",
    sort: str = "name"
):
    """List collections from DogeFeed.
    
    Uses explorer.dogesoft.io for structured collection data since dogefeed.app
    doesn't expose a public collection listing API. Images are served from
    dogefeed.app's /doginal-img/ endpoint.
    
    Each collection includes `sample_item_ids` - IDs of actual items from the
    collection to use as representative icons (falls back to generic icon if empty).
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    
    cache_key = f"collections:{offset}:{limit}:{q}:{sort}"
    cached = _get_cached(_collection_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        params = {"offset": offset, "limit": limit}
        data = await _fetch_dogefeed_explorer("/explorer/collections", params)
        
        collections_raw = data if isinstance(data, list) else data.get("collections", data.get("items", []))
        
        collections = []
        for c in collections_raw:
            coll = {
                "id": c.get("id") or c.get("collection_id") or "",
                "name": c.get("name") or c.get("title") or "Unnamed",
                "description": c.get("description") or "",
                "item_count": c.get("item_count") or c.get("supply") or c.get("count") or 0,
                "holder_count": c.get("holder_count") or c.get("holders") or 0,
                "cover_id": c.get("cover_id") or c.get("cover") or c.get("icon") or "",
                "created_at": c.get("created_at") or c.get("timestamp") or None,
                "sample_item_ids": [],  # Will be populated below
            }
            if q and q.lower() not in (coll["name"] or "").lower():
                continue
            collections.append(coll)
        
        if sort == "holders":
            collections.sort(key=lambda x: -(x.get("holder_count") or 0))
        elif sort == "recent":
            collections.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        else:
            collections.sort(key=lambda x: (x.get("name") or "").lower())
        
        # Fetch sample items for collections that need icons (up to first 20)
        import asyncio
        collections_to_fetch = collections[:min(limit, 20)]
        sample_tasks = [
            _fetch_collection_sample_items(c["id"], count=3)
            for c in collections_to_fetch
            if c["id"]
        ]
        if sample_tasks:
            sample_results = await asyncio.gather(*sample_tasks, return_exceptions=True)
            idx = 0
            for c in collections_to_fetch:
                if c["id"]:
                    result = sample_results[idx]
                    if isinstance(result, list):
                        c["sample_item_ids"] = result
                    idx += 1
        
        result = {
            "collections": collections[:limit],
            "total": len(collections_raw),
            "offset": offset,
            "limit": limit,
            "has_more": len(collections_raw) > offset + limit,
            "source": "explorer.dogesoft.io",
            "image_base": DOGEFEED_IMG_BASE,
        }
        
        _set_cached(_collection_cache, cache_key, result)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching collections")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/collection/items")
async def api_collection_items(
    id: str = "",
    offset: int = 0,
    limit: int = 50
):
    """Get items in a collection from DogeFeed.
    
    Uses explorer.dogesoft.io for item listings. Images served from dogefeed.app.
    """
    if not id:
        raise HTTPException(status_code=400, detail="Collection ID required")
    
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    
    cache_key = f"items:{id}:{offset}:{limit}"
    cached = _get_cached(_collection_items_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        data = await _fetch_dogefeed_explorer(f"/explorer/collections/{id}/items", {"offset": offset, "limit": limit})
        
        items_raw = data if isinstance(data, list) else data.get("items", data.get("inscriptions", []))
        
        items = []
        for i in items_raw:
            item = {
                "id": i.get("id") or i.get("inscription_id") or "",
                "number": i.get("number") or i.get("inscription_number"),
                "name": i.get("name") or i.get("title") or "",
                "content_type": i.get("content_type") or i.get("mime_type") or "",
                "owner": i.get("owner") or i.get("address") or "",
                "genesis_height": i.get("genesis_height") or i.get("height"),
            }
            items.append(item)
        
        result = {
            "items": items[:limit],
            "collection_id": id,
            "offset": offset,
            "limit": limit,
            "has_more": len(items_raw) > limit,
            "source": "explorer.dogesoft.io",
            "image_base": DOGEFEED_IMG_BASE,
        }
        
        _set_cached(_collection_items_cache, cache_key, result)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching collection items")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/inscription")
async def api_inscription(id: str = ""):
    """Get inscription details.
    
    Uses explorer.dogesoft.io for metadata. Image available at dogefeed.app/doginal-img/{id}.
    """
    if not id:
        raise HTTPException(status_code=400, detail="Inscription ID required")
    
    try:
        data = await _fetch_dogefeed_explorer(f"/explorer/inscriptions/{id}")
        
        inscription = {
            "id": data.get("id") or id,
            "number": data.get("number") or data.get("inscription_number"),
            "content_type": data.get("content_type") or data.get("mime_type") or "",
            "content_size": data.get("content_size") or data.get("size"),
            "owner": data.get("owner") or data.get("address") or "",
            "genesis_height": data.get("genesis_height") or data.get("height"),
            "collection": data.get("collection") or data.get("collection_name") or "",
            "image_url": f"{DOGEFEED_IMG_BASE}/{id}",
        }
        
        return {"inscription": inscription, "source": "explorer.dogesoft.io", "image_base": DOGEFEED_IMG_BASE}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching inscription")
        raise HTTPException(status_code=500, detail=str(e))


# --- DRC-20 Tokens API ---

_token_cache: dict[str, tuple[float, Any]] = {}


@router.get("/api/tokens")
async def api_tokens(
    offset: int = 0,
    limit: int = 50,
    q: str = "",
    sort: str = "holders"
):
    """List DRC-20 tokens from DogeFeed.
    
    Returns token tickers with supply, holders, and mint progress.
    Uses dogefeed.app where available, falls back to explorer.dogesoft.io.
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    
    cache_key = f"tokens:{offset}:{limit}:{q}:{sort}"
    cached = _get_cached(_token_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        # Try dogefeed.app first for DRC-20 data
        try:
            data = await _fetch_dogefeed_app("/api/drc20/tokens", {"offset": offset, "limit": limit})
        except HTTPException:
            # Fallback to explorer
            data = await _fetch_dogefeed_explorer("/drc20/tokens", {"offset": offset, "limit": limit})
        
        tokens_raw = data if isinstance(data, list) else data.get("tokens", data.get("items", []))
        
        tokens = []
        for t in tokens_raw:
            # Calculate mint progress percentage
            max_supply = t.get("max_supply") or t.get("max") or t.get("limit") or 0
            minted = t.get("minted") or t.get("total_minted") or t.get("current_supply") or 0
            if isinstance(max_supply, str):
                try:
                    max_supply = int(max_supply)
                except ValueError:
                    max_supply = 0
            if isinstance(minted, str):
                try:
                    minted = int(minted)
                except ValueError:
                    minted = 0
            
            mint_progress = 0
            if max_supply > 0:
                mint_progress = min(100, round((minted / max_supply) * 100, 1))
            
            token = {
                "ticker": t.get("ticker") or t.get("tick") or t.get("name") or "",
                "deploy_inscription": t.get("deploy_inscription") or t.get("inscription_id") or t.get("id") or "",
                "max_supply": max_supply,
                "minted": minted,
                "mint_progress": mint_progress,
                "holder_count": t.get("holder_count") or t.get("holders") or 0,
                "transfer_count": t.get("transfer_count") or t.get("transfers") or 0,
                "deploy_time": t.get("deploy_time") or t.get("deployed_at") or t.get("timestamp") or None,
                "decimals": t.get("decimals") or t.get("dec") or 18,
                "mint_limit": t.get("mint_limit") or t.get("lim") or None,
            }
            
            if q and q.lower() not in (token["ticker"] or "").lower():
                continue
            tokens.append(token)
        
        if sort == "holders":
            tokens.sort(key=lambda x: -(x.get("holder_count") or 0))
        elif sort == "recent":
            tokens.sort(key=lambda x: x.get("deploy_time") or "", reverse=True)
        elif sort == "progress":
            tokens.sort(key=lambda x: -(x.get("mint_progress") or 0))
        else:
            tokens.sort(key=lambda x: (x.get("ticker") or "").lower())
        
        result = {
            "tokens": tokens[:limit],
            "total": len(tokens_raw),
            "offset": offset,
            "limit": limit,
            "has_more": len(tokens_raw) > offset + limit,
            "source": "dogefeed.app",
        }
        
        _set_cached(_token_cache, cache_key, result)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching tokens")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/token")
async def api_token(ticker: str = ""):
    """Get details for a specific DRC-20 token."""
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker required")
    
    cache_key = f"token:{ticker}"
    cached = _get_cached(_token_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        try:
            data = await _fetch_dogefeed_app(f"/api/drc20/token/{ticker}")
        except HTTPException:
            data = await _fetch_dogefeed_explorer(f"/drc20/token/{ticker}")
        
        max_supply = data.get("max_supply") or data.get("max") or data.get("limit") or 0
        minted = data.get("minted") or data.get("total_minted") or data.get("current_supply") or 0
        if isinstance(max_supply, str):
            try:
                max_supply = int(max_supply)
            except ValueError:
                max_supply = 0
        if isinstance(minted, str):
            try:
                minted = int(minted)
            except ValueError:
                minted = 0
        
        mint_progress = 0
        if max_supply > 0:
            mint_progress = min(100, round((minted / max_supply) * 100, 1))
        
        token = {
            "ticker": data.get("ticker") or data.get("tick") or ticker,
            "deploy_inscription": data.get("deploy_inscription") or data.get("inscription_id") or data.get("id") or "",
            "max_supply": max_supply,
            "minted": minted,
            "mint_progress": mint_progress,
            "holder_count": data.get("holder_count") or data.get("holders") or 0,
            "transfer_count": data.get("transfer_count") or data.get("transfers") or 0,
            "deploy_time": data.get("deploy_time") or data.get("deployed_at") or data.get("timestamp") or None,
            "decimals": data.get("decimals") or data.get("dec") or 18,
            "mint_limit": data.get("mint_limit") or data.get("lim") or None,
            "deployer": data.get("deployer") or data.get("creator") or "",
        }
        
        result = {"token": token, "source": "dogefeed.app"}
        _set_cached(_token_cache, cache_key, result)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching token details")
        raise HTTPException(status_code=500, detail=str(e))


# --- Names API ---

_names_cache: dict[str, tuple[float, Any]] = {}


@router.get("/api/names")
async def api_names(
    offset: int = 0,
    limit: int = 50,
    q: str = "",
    sort: str = "recent"
):
    """List .doge/.dogefeed names from DogeFeed.
    
    Returns names with owner, inscription info, and registration details.
    Uses dogefeed.app where available, falls back to explorer.dogesoft.io.
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    
    cache_key = f"names:{offset}:{limit}:{q}:{sort}"
    cached = _get_cached(_names_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        # Try dogefeed.app first
        try:
            data = await _fetch_dogefeed_app("/api/names", {"offset": offset, "limit": limit, "q": q})
        except HTTPException:
            # Fallback to explorer
            data = await _fetch_dogefeed_explorer("/names", {"offset": offset, "limit": limit})
        
        if isinstance(data, list):
            names_raw = data
        else:
            # dogefeed.app /api/names returns {records:[{dns, address, url}]}.
            names_raw = data.get("records") or data.get("names") or data.get("items") or []
        
        names = []
        for n in names_raw:
            name = {
                "name": n.get("dns") or n.get("name") or n.get("domain") or "",
                "inscription_id": n.get("inscription_id") or n.get("id") or "",
                "inscription_number": n.get("inscription_number") or n.get("number"),
                "owner": n.get("owner") or n.get("holder") or n.get("address") or "",
                "registered_at": n.get("registered_at") or n.get("inscribed_at") or n.get("timestamp") or None,
                "genesis_height": n.get("genesis_height") or n.get("height"),
                "suffix": ".doge" if (n.get("name") or "").endswith(".doge") else ".dogefeed" if (n.get("name") or "").endswith(".dogefeed") else "",
            }
            
            if q and q.lower() not in (name["name"] or "").lower():
                continue
            names.append(name)
        
        if sort == "recent":
            names.sort(key=lambda x: x.get("registered_at") or "", reverse=True)
        elif sort == "alpha":
            names.sort(key=lambda x: (x.get("name") or "").lower())
        else:
            names.sort(key=lambda x: x.get("registered_at") or "", reverse=True)
        
        result = {
            "names": names[:limit],
            "total": len(names_raw),
            "offset": offset,
            "limit": limit,
            "has_more": len(names_raw) > offset + limit,
            "source": "dogefeed.app",
        }
        
        _set_cached(_names_cache, cache_key, result)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching names")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/name")
async def api_name(name: str = ""):
    """Get details for a specific .doge/.dogefeed name."""
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    
    cache_key = f"name:{name}"
    cached = _get_cached(_names_cache, cache_key)
    if cached is not None:
        return cached
    
    try:
        try:
            data = await _fetch_dogefeed_app(f"/api/name/{name}")
        except HTTPException:
            data = await _fetch_dogefeed_explorer(f"/name/{name}")
        
        if not data or not data.get("name"):
            return {"found": False, "name": name}
        
        name_info = {
            "name": data.get("name") or name,
            "inscription_id": data.get("inscription_id") or data.get("id") or "",
            "inscription_number": data.get("inscription_number") or data.get("number"),
            "owner": data.get("owner") or data.get("holder") or data.get("address") or "",
            "registered_at": data.get("registered_at") or data.get("inscribed_at") or data.get("timestamp") or None,
            "genesis_height": data.get("genesis_height") or data.get("height"),
            "url": data.get("url") or "",
            "suffix": ".doge" if name.endswith(".doge") else ".dogefeed" if name.endswith(".dogefeed") else "",
        }
        
        result = {"found": True, "name_info": name_info, "source": "dogefeed.app"}
        _set_cached(_names_cache, cache_key, result)
        return result
        
    except HTTPException:
        return {"found": False, "name": name}
    except Exception as e:
        logger.warning(f"Error fetching name details: {e}")
        return {"found": False, "name": name}


@router.get("/api/search")
async def api_search(q: str = "", type: str = "all"):
    """Search DogeFeed using dogefeed.app's search API.
    
    Searches users and posts. Use type=users, type=posts, or type=all.
    """
    if not q or len(q) < 2:
        raise HTTPException(status_code=400, detail="Query too short")
    
    cache_key = f"search:{q}:{type}"
    cached = _get_cached(_collection_cache, cache_key, ttl=120)  # 2 min cache
    if cached is not None:
        return cached
    
    try:
        params = {"q": q}
        if type in ("users", "posts"):
            params["type"] = type
        data = await _fetch_dogefeed_app("/api/search", params)
        
        users = data.get("users", [])
        posts = data.get("posts", [])
        
        doginal_users = []
        for u in users:
            avatar = u.get("avatarUrl", "")
            inscription_id = ""
            if avatar.startswith("/doginal-img/"):
                inscription_id = avatar.replace("/doginal-img/", "")
            doginal_users.append({
                "handle": u.get("handle", ""),
                "display_name": u.get("displayName", ""),
                "verified_pfp": u.get("verifiedPfp", False),
                "pfp_rarity": u.get("pfpRarity"),
                "inscription_id": inscription_id,
                "avatar_url": f"{DOGEFEED_APP_BASE}{avatar}" if avatar.startswith("/") else avatar,
            })
        
        result = {
            "query": q,
            "users": doginal_users,
            "posts": posts[:20],
            "source": "dogefeed.app",
            "image_base": DOGEFEED_IMG_BASE,
        }
        
        _set_cached(_collection_cache, cache_key, result)
        return result
        
    except HTTPException:
        return {"query": q, "users": [], "posts": [], "source": "dogefeed.app", "error": "unreachable"}
    except Exception as e:
        logger.warning(f"Error searching: {e}")
        return {"query": q, "users": [], "posts": [], "source": "dogefeed.app", "error": str(e)}


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
    """Check address balance using DogeFeed Explorer API."""
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    
    try:
        data = await _fetch_dogefeed_explorer(f"/wallet/address/{address}/balance")
        
        balances = [{
            "chain": "Dogecoin",
            "amount": str(data.get("balance", 0) / 100000000),
            "symbol": "DOGE",
        }]
        
        return {"address": address, "balances": balances}
        
    except HTTPException:
        return {"address": address, "balances": []}
    except Exception:
        return {"address": address, "balances": []}


# Cache for address inscriptions
_address_inscriptions_cache: dict[str, tuple[float, Any]] = {}


@router.get("/api/address/inscriptions")
async def api_address_inscriptions(
    address: str = "",
    offset: int = 0,
    limit: int = 50
):
    """Get inscriptions held by an address.
    
    This is the primary endpoint for the Wallet view to display inscriptions.
    Uses dogefeed.app and explorer.dogesoft.io for inscription data.
    
    SECURITY: Read-only. Returns inscription metadata only, never UTXOs or
    spending data. Per exchange-safety.md, inscription-carrying UTXOs must
    never be exposed to spending logic.
    """
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    
    limit = min(max(1, limit), 100)
    offset = max(0, offset)
    
    cache_key = f"addr_inscriptions:{address}:{offset}:{limit}"
    cached = _get_cached(_address_inscriptions_cache, cache_key, ttl=120)
    if cached is not None:
        return cached
    
    try:
        # Try explorer API for address inscriptions
        data = await _fetch_dogefeed_explorer(
            f"/wallet/address/{address}/inscriptions",
            {"offset": offset, "limit": limit}
        )
        
        inscriptions_raw = data if isinstance(data, list) else data.get("inscriptions", data.get("items", []))
        
        inscriptions = []
        for i in inscriptions_raw:
            insc = {
                "id": i.get("id") or i.get("inscription_id") or "",
                "number": i.get("number") or i.get("inscription_number"),
                "name": i.get("name") or i.get("title") or "",
                "content_type": i.get("content_type") or i.get("mime_type") or "image/png",
                "content_size": i.get("content_size") or i.get("size"),
                "collection": i.get("collection") or i.get("collection_name") or "",
                "genesis_height": i.get("genesis_height") or i.get("height"),
                "image_url": f"{DOGEFEED_IMG_BASE}/{i.get('id') or i.get('inscription_id') or ''}",
            }
            inscriptions.append(insc)
        
        result = {
            "address": address,
            "inscriptions": inscriptions,
            "total": data.get("total", len(inscriptions_raw)) if isinstance(data, dict) else len(inscriptions_raw),
            "offset": offset,
            "limit": limit,
            "has_more": len(inscriptions_raw) >= limit,
            "source": "explorer.dogesoft.io",
            "image_base": DOGEFEED_IMG_BASE,
        }
        
        _set_cached(_address_inscriptions_cache, cache_key, result)
        return result
        
    except HTTPException:
        return {
            "address": address,
            "inscriptions": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "source": "explorer.dogesoft.io",
            "error": "unreachable",
        }
    except Exception as e:
        logger.warning(f"Error fetching address inscriptions: {e}")
        return {
            "address": address,
            "inscriptions": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "source": "explorer.dogesoft.io",
            "error": str(e),
        }


@router.get("/api/address/tokens")
async def api_address_tokens(address: str = ""):
    """Get DRC-20 token balances for an address.
    
    SECURITY: Read-only. Returns token balances only.
    """
    if not address:
        raise HTTPException(status_code=400, detail="Address required")
    
    cache_key = f"addr_tokens:{address}"
    cached = _get_cached(_token_cache, cache_key, ttl=120)
    if cached is not None:
        return cached
    
    try:
        # Try dogefeed.app first
        try:
            data = await _fetch_dogefeed_app(f"/api/address/{address}/drc20")
        except HTTPException:
            data = await _fetch_dogefeed_explorer(f"/drc20/address/{address}")
        
        tokens_raw = data if isinstance(data, list) else data.get("tokens", data.get("balances", []))
        
        tokens = []
        for t in tokens_raw:
            token = {
                "ticker": t.get("ticker") or t.get("tick") or "",
                "balance": t.get("balance") or t.get("amount") or "0",
                "transferable": t.get("transferable") or "0",
                "available": t.get("available") or t.get("balance") or "0",
            }
            tokens.append(token)
        
        result = {
            "address": address,
            "tokens": tokens,
            "source": "dogefeed.app",
        }
        
        _set_cached(_token_cache, cache_key, result)
        return result
        
    except HTTPException:
        return {"address": address, "tokens": [], "source": "dogefeed.app", "error": "unreachable"}
    except Exception as e:
        logger.warning(f"Error fetching address tokens: {e}")
        return {"address": address, "tokens": [], "source": "dogefeed.app", "error": str(e)}


@router.get("/api/lookup")
async def api_lookup(name: str = ""):
    """Look up a name (e.g., john.doge) using DogeFeed Explorer API."""
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    
    try:
        data = await _fetch_dogefeed_explorer(f"/name/{name}")
        
        if data and data.get("name"):
            return {
                "found": True,
                "name": data.get("name"),
                "owner": data.get("owner") or data.get("holder") or "",
                "address": data.get("address") or "",
                "url": data.get("url") or "",
                "inscribed_at": data.get("inscribed_at") or data.get("height"),
            }
        else:
            return {"found": False, "name": name}
            
    except HTTPException:
        return {"found": False, "name": name}
    except Exception:
        return {"found": False, "name": name}


@router.get("/api/memes")
async def api_memes():
    """Public meme library, if dogefeed.app publishes one. Empty when it does not."""
    from .public_read import empty, fetch_json

    async def _one(path: str):
        try:
            return await fetch_json(path)
        except Exception:
            return None

    for data in await asyncio.gather(_one("/api/memes"), _one("/api/meme")):
        if not data:
            continue
        rows = data if isinstance(data, list) else data.get("memes") or data.get("items") or []
        memes = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            memes.append({
                "id": row.get("id") or "",
                "name": row.get("name") or row.get("title") or "",
                "image": row.get("image") or row.get("url") or row.get("src") or "",
            })
        if memes:
            return {"memes": memes, "source": "dogefeed.app"}
    from .public_read import fetch_html, parse_memes

    try:
        page = await fetch_html("/memes")
    except Exception:
        page = ""
    rows = parse_memes(page) if page else []
    if rows:
        return {"memes": rows, "source": "dogefeed.app"}
    return empty("no_public_list", memes=[])


@router.get("/api/launchpad")
async def api_launchpad():
    """Public minting-now cards from dogefeed.app/launchpad. Empty when the page has none."""
    from .public_read import empty, fetch_html, parse_launchpad

    try:
        page = await fetch_html("/launchpad")
    except Exception:
        return empty("no_public_list", launches=[])
    rows = parse_launchpad(page)
    if not rows:
        return empty("no_public_list", launches=[])
    return {"launches": rows, "source": "dogefeed.app"}


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


def register_routes(app, render):
    """Register DogeFeedX routes with the main app.
    
    Call this from app.py to add the DogeFeedX pages and API endpoints.
    
    Args:
        app: The FastAPI application instance
        render: The template rendering function from app.py
    """
    app.include_router(router)
    
    @app.get("/dogefeedx/explorer", response_class=HTMLResponse)
    def dogefeedx_explorer(request: Request):
        """The DogeFeed Explorer app - Finder-style inscription browser."""
        return render(request, "dogefeedx/explorer.html", fullscreen=True)
    
    @app.get("/dogefeedx/terminal", response_class=HTMLResponse)
    def dogefeedx_terminal(request: Request):
        """The DogeFeed Terminal app - Terminal.app-style command window."""
        return render(request, "dogefeedx/terminal.html", fullscreen=True)
    
    @app.get("/dogefeedx/feed", response_class=HTMLResponse)
    def dogefeedx_feed(request: Request):
        """The DogeFeed social feed - Twitter/X-style post timeline."""
        return render(request, "dogefeedx/feed.html", fullscreen=True)
    
    @app.get("/dogefeedx/profile", response_class=HTMLResponse)
    def dogefeedx_profile(request: Request):
        """The DogeFeed profile hub - user profiles with holdings and activity."""
        return render(request, "dogefeedx/profile.html", fullscreen=True)
    
    @app.get("/dogefeedx/explore", response_class=HTMLResponse)
    def dogefeedx_explore(request: Request):
        """The DogeFeed explore page - trending, people to follow, collections."""
        return render(request, "dogefeedx/explore.html", fullscreen=True)
    
    @app.get("/dogefeedx/market", response_class=HTMLResponse)
    def dogefeedx_market(request: Request):
        """The DogeFeed market - listings sorted cheapest first."""
        return render(request, "dogefeedx/market.html", fullscreen=True)
    
    @app.get("/dogefeedx/wallet", response_class=HTMLResponse)
    def dogefeedx_wallet(request: Request):
        """The DogeFeed wallet - read-only view of inscriptions by address."""
        return render(request, "dogefeedx/wallet.html", fullscreen=True)
    
    @app.get("/dogefeedx/inscription", response_class=HTMLResponse)
    def dogefeedx_inscription(request: Request):
        """The DogeFeed inscription detail - full content viewer for any inscription."""
        return render(request, "dogefeedx/inscription.html", fullscreen=True)
    
    @app.get("/dogefeedx/leaderboard", response_class=HTMLResponse)
    def dogefeedx_leaderboard(request: Request):
        """The DogeFeed leaderboard - top users by activity and holdings."""
        return render(request, "dogefeedx/leaderboard.html", fullscreen=True)
    
    @app.get("/dogefeedx/notifications", response_class=HTMLResponse)
    def dogefeedx_notifications(request: Request):
        """The DogeFeed notifications - activity alerts (requires sign-in)."""
        return render(request, "dogefeedx/notifications.html", fullscreen=True)
    
    @app.get("/dogefeedx/messages", response_class=HTMLResponse)
    def dogefeedx_messages(request: Request):
        """The DogeFeed messages - direct messages (requires sign-in)."""
        return render(request, "dogefeedx/messages.html", fullscreen=True)
    
    @app.get("/dogefeedx/bookmarks", response_class=HTMLResponse)
    def dogefeedx_bookmarks(request: Request):
        """The DogeFeed bookmarks - saved posts (requires sign-in)."""
        return render(request, "dogefeedx/bookmarks.html", fullscreen=True)
    
    @app.get("/dogefeedx/collections", response_class=HTMLResponse)
    def dogefeedx_collections(request: Request):
        """The DogeFeed collections - browse all Doginal collections."""
        return render(request, "dogefeedx/collections.html", fullscreen=True)
    
    @app.get("/dogefeedx/nfts", response_class=HTMLResponse)
    def dogefeedx_nfts(request: Request):
        """The DogeFeed NFTs page - browse and discover NFTs."""
        return render(request, "dogefeedx/nfts.html", fullscreen=True)
    
    @app.get("/dogefeedx/launchpad", response_class=HTMLResponse)
    def dogefeedx_launchpad(request: Request):
        """The DogeFeed launchpad - new project launches and mints."""
        return render(request, "dogefeedx/launchpad.html", fullscreen=True)
    
    @app.get("/dogefeedx/search", response_class=HTMLResponse)
    def dogefeedx_search(request: Request):
        """The DogeFeed search page - global search results."""
        return render(request, "dogefeedx/search.html", fullscreen=True)
    
    @app.get("/dogefeedx/post", response_class=HTMLResponse)
    def dogefeedx_post(request: Request):
        """The DogeFeed post detail - individual post view with replies."""
        return render(request, "dogefeedx/post.html", fullscreen=True)

    @app.get("/dogefeedx/files", response_class=HTMLResponse)
    def dogefeedx_files(request: Request):
        """Finder-style view of this arcade wallet: collections, DRC-20 and names."""
        return render(request, "dogefeedx/files.html", fullscreen=True)

    @app.get("/dogefeedx/inscribe", response_class=HTMLResponse)
    def dogefeedx_inscribe(request: Request):
        """Inscribe a collection, a DRC-20 or a name with this arcade's wallet."""
        return render(request, "dogefeedx/inscribe.html", fullscreen=True)

    @app.get("/dogefeedx/arcade", response_class=HTMLResponse)
    def dogefeedx_arcade(request: Request):
        """Games on this arcade. The player is the on-chain account."""
        return render(request, "dogefeedx/arcade.html", fullscreen=True)

    @app.get("/dogefeedx/memes", response_class=HTMLResponse)
    def dogefeedx_memes(request: Request):
        """Meme library. Public reads only; empty when dogefeed.app has no public list."""
        return render(request, "dogefeedx/memes.html", fullscreen=True)

    @app.get("/dogefeedx/settings", response_class=HTMLResponse)
    def dogefeedx_settings(request: Request):
        """Settings kept on this arcade. Nothing is written to dogefeed.app."""
        return render(request, "dogefeedx/settings.html", fullscreen=True)
