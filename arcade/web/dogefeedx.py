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

@router.get("/aqua.css")
async def aqua_css():
    """Serve the Aqua theme CSS."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "aqua.css"
    if path.is_file():
        return FileResponse(path, media_type="text/css")
    raise HTTPException(status_code=404)


@router.get("/shell.js")
async def shell_js():
    """Serve the shell registration JavaScript."""
    path = Path(__file__).parent / "templates" / "dogefeedx" / "shell.js"
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


# --- Explorer API ---

@router.get("/api/feed")
async def api_feed(
    limit: int = 20,
    cursor: str = ""
):
    """Get the DogeFeed social feed from dogefeed.app.
    
    Returns posts from verified doginal holders and recent activity.
    This is the primary dogefeed.app data source.
    """
    limit = min(max(1, limit), 50)
    
    cache_key = f"feed:{cursor}:{limit}"
    cached = _get_cached(_collection_cache, cache_key, ttl=60)  # 1 min cache for feed
    if cached is not None:
        return cached
    
    try:
        params = {"tab": "for-you", "limit": limit}
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
