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
    """GET one public HTML page. Only the public boards and a profile."""
    allowed = path in ("/", "/market", "/leaderboard", "/memes", "/launchpad", "/arcade") or (
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
    payload = _market_payload(html)
    if payload["collections"]:
        collections = payload["collections"]
    doginals = payload["totals"].get("doginals")
    if doginals:
        stats = {
            "collections": str(doginals["collections"]),
            "items": doginals["itemsLabel"],
            "listings": doginals["listingsLabel"],
            "volume": doginals["totalTraded"],
        }
    result = {"stats": stats, "collections": collections, "source": "dogefeed.app"}
    if payload["totals"]:
        result["totals"] = payload["totals"]
    if payload["activity"]:
        result["activity"] = payload["activity"]
    return result


_ESC = r"(?:\\.|[^\\])*?"


def _unesc(value: str) -> str:
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value or "",
    )
    return unescape(text.replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\")).strip()


def _market_payload(html: str) -> dict:
    """Collections, chain totals and recent sales embedded in the public /market page.

    The visible cards are only the Doginals NFT list. DNS, DogeFeed Exclusive and
    Robinhood live in the same page payload, with the flags the tabs filter on.
    """
    collections = []
    seen: set[str] = set()
    cards = re.compile(
        r'\\"slug\\":\\"([a-z0-9-]+)\\",\\"name\\":\\"(' + _ESC + r')\\",\\"description\\":\\"'
        + _ESC + r'\\",\\"cover\\":\\"([^\\]*?)\\",\\"verified\\":(true|false),\\"exclusive\\":(true|false),'
        r'\\"mintOpen\\":(true|false),\\"network\\":\\"([^\\]*?)\\",\\"items\\":(\d+),\\"listed\\":(\d+),'
        r'\\"owners\\":(\d+),\\"sales\\":(\d+),\\"floorLabel\\":\\"([^\\]*?)\\",\\"volumeLabel\\":\\"([^\\]*?)\\",'
        r'\\"floorSort\\":(-?\d+(?:\.\d+)?),\\"volumeSort\\":(-?\d+(?:\.\d+)?),\\"createdAt\\":\\"([^\\]*?)\\",'
        r'\\"isDoginal\\":(true|false),\\"isSns\\":(true|false),\\"snsOrder\\":(-?\d+),\\"chain\\":\\"([^\\"]+)\\"'
    )
    for row in cards.findall(html or ""):
        slug = row[0]
        if slug in seen or not _SLUG.fullmatch(slug):
            continue
        seen.add(slug)
        collections.append({
            "slug": slug,
            "href": f"/market/{slug}",
            "name": _unesc(row[1]),
            "image": _unesc(row[2]),
            "floor": _unesc(row[11]) or None,
            "listed": int(row[8]),
            "sales": int(row[10]),
            "verified": row[3] == "true",
            "exclusive": row[4] == "true",
            "sns": row[17] == "true",
            "chain": row[19],
            "created": row[15],
        })

    totals = {}
    for key in ("doginals", "robinhood", "sns"):
        match = re.search(
            rf'\\"{key}\\":\{{' + r'\\"collections\\":(\d+),\\"items\\":(\d+),\\"liveListings\\":(\d+),'
            r'\\"itemsLabel\\":\\"([^\\]*)\\",\\"listingsLabel\\":\\"([^\\]*)\\",\\"totalTraded\\":\\"([^\\]*)\\"',
            html or "",
        )
        if not match:
            continue
        totals[key] = {
            "collections": int(match.group(1)),
            "items": int(match.group(2)),
            "liveListings": int(match.group(3)),
            "itemsLabel": _unesc(match.group(4)),
            "listingsLabel": _unesc(match.group(5)),
            "totalTraded": _unesc(match.group(6)),
        }

    activity = []
    seen_href: set[str] = set()
    sales = re.compile(
        r'\\"kind\\":\\"([a-z]+)\\",\\"listingId\\":\\"([^\\]*)\\",\\"inscriptionId\\":\\"([^\\]*)\\",'
        r'\\"imageUrl\\":\\"([^\\]*)\\",\\"network\\":\\"([^\\]*)\\",\\"number\\":(null|\d+),'
        r'\\"name\\":\\"(' + _ESC + r')\\",\\"collectionSlug\\":\\"([^\\]*)\\",\\"collectionName\\":\\"('
        + _ESC + r')\\",\\"priceDoge\\":\\"([^\\]*)\\",\\"priceLabel\\":\\"([^\\]*)\\",'
        r'\\"usdLabel\\":(null|\\"[^\\]*\\"),\\"at\\":\\"([^\\]*)\\",\\"href\\":\\"([^\\]*)\\"'
    )
    for row in sales.findall(html or ""):
        href = row[13]
        if not href.startswith("/market/") or href in seen_href:
            continue
        seen_href.add(href)
        activity.append({
            "kind": row[0],
            "name": _unesc(row[6]),
            "image": _unesc(row[3]),
            "collection": _unesc(row[8]),
            "price": _unesc(row[10]),
            "href": href,
            "at": row[12],
        })
        if len(activity) >= 40:
            break
    return {"collections": collections, "totals": totals, "activity": activity}


def parse_memes(html: str, limit: int = 48) -> list[dict]:
    """Public meme cards from /memes. Empty when the grid is absent."""
    found: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'href="(/memes/[A-Za-z0-9]+)"[^>]*>\s*<img src="(/api/media/[^"]+)" alt="([^"]*)"',
        html,
    ):
        image = match.group(2)
        if image in seen:
            continue
        seen.add(image)
        found.append({
            "id": match.group(1).rsplit("/", 1)[-1],
            "name": unescape(match.group(3)).strip(),
            "image": image,
        })
        if len(found) >= limit:
            break
    return found


def _meme_image(url: str) -> str:
    """A public meme picture. Anything else is dropped so a tile cannot be text-only."""
    url = (url or "").strip()
    if ".." in url or url.startswith("//"):
        return ""
    if url.startswith("/api/media/"):
        return url
    prefix = DOGEFEED_ORIGIN + "/api/media/"
    if url.startswith(prefix):
        return url
    return ""


def normalize_memes(data) -> list[dict]:
    """Rows from /api/memes. The picture field on that route is `imageUrl`, the title is `title`."""
    rows = data if isinstance(data, list) else (data or {}).get("items") or (data or {}).get("memes") or []
    found = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        image = _meme_image(str(row.get("imageUrl") or row.get("image") or row.get("url") or row.get("src") or ""))
        if not image or image in seen:
            continue
        seen.add(image)
        tags = row.get("tags") if isinstance(row.get("tags"), list) else []
        try:
            uses = int(row.get("uses") or 0)
        except (TypeError, ValueError):
            uses = 0
        found.append({
            "id": unescape(str(row.get("id") or "")).strip(),
            "name": unescape(str(row.get("title") or row.get("name") or "")).strip(),
            "image": image,
            "format": unescape(str(row.get("format") or "")).strip(),
            "category": unescape(str(row.get("category") or "")).strip(),
            "tags": [str(tag) for tag in tags if isinstance(tag, str)][:12],
            "uses": uses,
            "sourceName": unescape(str(row.get("sourceName") or "")).strip(),
        })
    return found


def parse_meme_count(html: str) -> dict | None:
    """The public pager, e.g. 968 memes on page 1 of 9. None when the page has no count."""
    match = re.search(
        r'data-testid="meme-count">(\d+)[\s\S]{0,120}?of <!-- -->(\d+)',
        html or "",
    )
    if not match:
        return None
    return {"count": int(match.group(1)), "pages": int(match.group(2))}


def parse_arcade(html: str) -> list[dict]:
    """Public game tiles from dogefeed.app/arcade. Empty when the grid is absent."""
    found = []
    seen: set[str] = set()
    for match in re.finditer(
        r'data-testid="arcade-tile-([a-z0-9-]+)" href="(/arcade/[a-z0-9-]+)"',
        html or "",
    ):
        slug, href = match.group(1), match.group(2)
        if slug in seen:
            continue
        chunk = (html or "")[match.start():match.start() + 8000]
        nxt = chunk.find("arcade-tile-", 20)
        if nxt > 0:
            chunk = chunk[:nxt]
        name = re.search(r'<p class="font-black">([^<]+)</p>', chunk)
        detail = re.search(r'<p class="text-xs[^"]*">([^<]+)</p>', chunk)
        if not name:
            continue
        seen.add(slug)
        found.append({
            "slug": slug,
            "href": href,
            "name": unescape(name.group(1)).strip(),
            "detail": unescape(detail.group(1)).strip() if detail else "",
        })
    return found


def _plain(chunk: str) -> str:
    text = re.sub(r"<!--.*?-->", "", chunk or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_launchpad(html: str) -> list[dict]:
    """Minting-now cards from the public launchpad. Empty when none are rendered."""
    found: list[dict] = []
    for match in re.finditer(
        r'data-testid="launchpad-card" href="(/launchpad/[a-z0-9-]+)"([\s\S]*?)</a>',
        html,
    ):
        href, body = match.group(1), match.group(2)
        name = re.search(r'font-black">([^<]+)</p>', body)
        image = re.search(r'<img src="([^"]+)"', body)
        detail = re.search(r'text-white/60">([\s\S]*?)</p>', body)
        progress = re.search(r'data-testid="launchpad-progress">([\s\S]*?)</span>', body)
        pct = re.search(r'data-testid="launchpad-progress-pct">([^<]+)', body)
        found.append({
            "slug": href.rsplit("/", 1)[-1],
            "name": _plain(name.group(1)) if name else "",
            "image": image.group(1) if image else "",
            "detail": _plain(detail.group(1)) if detail else "",
            "progress": _plain(progress.group(1)) if progress else "",
            "pct": _plain(pct.group(1)) if pct else "",
        })
    return found


def empty(error: str, **extra) -> dict:
    payload = {"source": "dogefeed.app", "error": error}
    payload.update(extra)
    return payload
