"""Read-only access to public dogefeed.app data.

Nothing here invents a user, a post, a count, or a price. A failed read
returns an empty payload and an error string. HTML parsers only pull
fields that dogefeed.app already renders on its public pages, because
those pages do not all have a JSON route.
"""
from __future__ import annotations

import re
from html import unescape

import httpx

DOGEFEED_ORIGIN = "https://dogefeed.app"
_TIMEOUT = 12.0
_HANDLE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_POST_ID = re.compile(r"^[A-Za-z0-9]{8,40}$")
_SLUG = re.compile(r"^[a-z0-9-]{1,80}$")


class PublicReadError(Exception):
    """dogefeed.app did not return usable public data."""


def valid_handle(handle: str) -> str:
    if not _HANDLE.fullmatch(handle or ""):
        raise PublicReadError("bad handle")
    return handle


def valid_post_id(post_id: str) -> str:
    if not _POST_ID.fullmatch(post_id or ""):
        raise PublicReadError("bad post id")
    return post_id


async def fetch_json(path: str, params: dict | None = None) -> dict:
    """GET a public JSON route on dogefeed.app. `path` must start with /api/."""
    if not path.startswith("/api/") or ".." in path:
        raise PublicReadError("refused path")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(
            DOGEFEED_ORIGIN + path,
            params=params or None,
            headers={"Accept": "application/json", "User-Agent": "DogecoinArcade-DogeFeedX-readonly"},
        )
        response.raise_for_status()
        data = response.json()
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"items": data}
    raise PublicReadError("unexpected payload")


_LEADERBOARD_RANGES = {"1d", "7d", "30d", "all"}


async def fetch_html(path: str, params: dict | None = None) -> str:
    """GET one public HTML page. Only /, /market, /leaderboard and /u/<handle>."""
    allowed = path in ("/", "/market", "/leaderboard") or (
        path.startswith("/u/") and _HANDLE.fullmatch(path[3:])
    )
    if not allowed:
        raise PublicReadError("refused page")
    query = None
    if params:
        if path != "/leaderboard" or set(params) != {"range"} or params.get("range") not in _LEADERBOARD_RANGES:
            raise PublicReadError("refused page")
        query = {"range": params["range"]}
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(
            DOGEFEED_ORIGIN + path,
            params=query,
            headers={"Accept": "text/html", "User-Agent": "DogecoinArcade-DogeFeedX-readonly"},
        )
        response.raise_for_status()
        return response.text


def parse_trending(html: str) -> list[dict]:
    """Hashtags from the public home sidebar. Empty when the markup is absent."""
    found: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="/tag/([A-Za-z0-9_]+)"([\s\S]{0,600}?)</a>', html):
        tag, body = match.group(1), match.group(2)
        if "Trending on DogeFeed" not in body or tag in seen:
            continue
        count = re.search(r"(\d+\s+posts?)", body)
        seen.add(tag)
        found.append({
            "rank": len(found) + 1,
            "tag": tag,
            "count": count.group(1) if count else "",
        })
    return found


def _earner_rows(body: str) -> list[dict]:
    """One row per <li>. A verified badge's SVG makes the <a> too long to slice by character count."""
    rows = []
    seen: set[str] = set()
    for item in re.finditer(r"<li[\s\S]*?</li>", body):
        chunk = item.group(0)
        handle = re.search(r'href="/u/([^"]+)"', chunk)
        name = re.search(r'font-bold">([^<]+)', chunk)
        amount = re.search(r"\+\s*(?:<!--\s*-->)?\s*([0-9.]+)\s*DOGE", chunk)
        avatar = re.search(r'<img src="([^"]+)"', chunk)
        if not handle or not name or not amount:
            continue
        who = handle.group(1)
        if who in seen or not _HANDLE.fullmatch(who):
            continue
        seen.add(who)
        rank = re.search(r">(\d+)</span>", chunk)
        rows.append({
            "rank": int(rank.group(1)) if rank else len(rows) + 1,
            "handle": who,
            "name": unescape(name.group(1)).strip(),
            "amount": "+" + amount.group(1) + " DOGE",
            "avatar": avatar.group(1) if avatar else "",
            "verified": "Verified" in chunk,
        })
    return rows


def parse_leaderboard(html: str) -> list[dict]:
    """7-day earners from the public home sidebar."""
    section = re.search(r"Leaderboard · 7D</h2><ol>([\s\S]*?)</ol>", html)
    if not section:
        return []
    return _earner_rows(section.group(1))


def parse_leaderboard_page(html: str) -> list[dict]:
    """Earners from a public /leaderboard page, for whichever range that page rendered."""
    main = re.search(
        r'href="/leaderboard\?range=all">ALL</a></nav><ol>([\s\S]*?)</ol>',
        html,
    )
    if main:
        return _earner_rows(main.group(1))
    return parse_leaderboard(html)


def parse_suggestions(html: str) -> list[dict]:
    """Who to follow, from the public home sidebar. Real accounts only."""
    section = re.search(r"Who to follow</h2><ul>([\s\S]*?)</ul>", html)
    if not section:
        return []
    people = []
    seen: set[str] = set()
    for item in re.finditer(r"<li[\s\S]*?</li>", section.group(1)):
        chunk = item.group(0)
        handle = re.search(r'href="/u/([^"]+)"', chunk)
        name = re.search(r'font-bold">([^<]+)', chunk)
        avatar = re.search(r'<img src="([^"]+)"', chunk)
        if not handle or not name:
            continue
        who = handle.group(1)
        if who in seen or not _HANDLE.fullmatch(who):
            continue
        seen.add(who)
        people.append({
            "handle": who,
            "name": unescape(name.group(1)).strip(),
            "avatar": avatar.group(1) if avatar else "",
            "verified": "Verified" in chunk,
        })
    return people


def parse_profile_card(html: str, handle: str) -> dict:
    """Follower counts and join month from a public /u/<handle> page."""
    followers = re.search(
        rf'href="/u/{re.escape(handle)}/followers"><b>(\d+)</b>', html
    )
    following = re.search(
        rf'href="/u/{re.escape(handle)}/following"><b>(\d+)</b>', html
    )
    joined = re.search(r"Joined <!-- -->([^<]+)", html)
    return {
        "followers": int(followers.group(1)) if followers else None,
        "following": int(following.group(1)) if following else None,
        "joined": unescape(joined.group(1)).strip() if joined else None,
    }


def parse_market(html: str) -> dict:
    """Doginal collection cards and the four market totals from /market."""
    stats = {}
    for label, key in (
        ("Collections", "collections"),
        ("Items", "items"),
        ("Live listings", "listings"),
        ("Total traded", "volume"),
    ):
        match = re.search(
            rf'>{re.escape(label)}</p><p class="truncate font-bold[^"]*"[^>]*>([^<]+)',
            html,
        )
        stats[key] = unescape(match.group(1)).strip() if match else None

    collections = []
    seen: set[str] = set()
    for match in re.finditer(r'href="(/market/([a-z0-9-]+))"([\s\S]*?)</a>', html):
        href, slug, body = match.group(1), match.group(2), match.group(3)
        if slug in {"offers"} or slug in seen or not _SLUG.fullmatch(slug):
            continue
        name = re.search(r'class="truncate">([^<]*)</span>', body)
        if not name:
            continue
        image = re.search(r'<img src="([^"]+)"', body)
        floor = re.search(r'Floor <b class="text-cream">([^<]*)</b>', body)
        listed = re.search(r"(\d+)\s*(?:<!--\s*-->\s*)*listed", body)
        sales = re.search(r"(\d+)\s*(?:<!--\s*-->\s*)*sales", body)
        seen.add(slug)
        collections.append({
            "slug": slug,
            "href": href,
            "name": unescape(name.group(1)).strip(),
            "image": image.group(1) if image else "",
            "floor": unescape(floor.group(1)).strip() if floor else None,
            "listed": int(listed.group(1)) if listed else 0,
            "sales": int(sales.group(1)) if sales else 0,
            "verified": 'aria-label="Verified"' in body,
        })
    return {"stats": stats, "collections": collections, "source": "dogefeed.app"}


def empty(error: str, **extra) -> dict:
    payload = {"source": "dogefeed.app", "error": error}
    payload.update(extra)
    return payload
