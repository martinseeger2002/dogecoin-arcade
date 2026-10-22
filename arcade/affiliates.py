"""Affiliates: the sites an arcade points its users at, and the ones linked arcades share with it (2026-10-21:
"I want an admin affiliate panel. It should show the operators published URLs with a check box next to each to share
with affiliates. It should have a list of shared URLs from other node operators, each with a checkmark to share on
client side affiliate tab." Which URLs: "Arcade's names + ones I add". And the same day: "The second list should be a
list of operators and their published URLs that they shared with the affiliates.").

Three kinds of site:

* **The arcade's own names** -- https://<host> for each of `state.public_hosts`. Listed automatically, never edited or
  removed here (they are the names the arcade is published under); each can be ticked to share. Never shown on this
  arcade's own Affiliates tab: a link to the page you are on is no affiliate.
* **The operator's own added sites** -- a name, an https URL and one line about it. Always on this arcade's Affiliates
  tab; each can be ticked to share. The fixed list the tab had before (SEED) is what an operator who never saved one
  has, so the tab keeps showing antidoginaldogs.com.
* **Sites other arcades share** -- said in the operators "me" frame (mesh/operators.py, field `sites`), kept per peer
  with when it was last heard. Nothing from another operator reaches this arcade's users until the operator ticks it
  (Show on my Affiliates tab, off by default), and a ticked site drops off the tab again once its sharer stops sharing
  it or has not been heard from in HEARD_DAYS.

Settings (settings.json):

    affiliates_mine          [{url, name, about, share}]   the operator's own added sites (SEED until first saved)
    affiliates_hosts_shared  [url]                         which of the arcade's own https://<host> names are shared
    affiliates_heard         {peer id: {at, tag, sites}}   what each linked arcade last said it shares
    affiliates_shown         [url]                         others' sites ticked to show on this arcade's tab

A site from the wire is checked strictly (`clean_site`): an https URL of at most 200 characters, parsed with urllib,
whose host is a DNS name (letters, digits, hyphens, dotted, a letter TLD) -- no userinfo, no port, no IP literal (an
affiliate is a named site; a bare address is how a link to somebody's router or a LAN box would arrive), no whitespace,
control characters, quotes, angle brackets or backslashes anywhere in it; a name of 1-60 and an about of 0-200
characters of plain text. Anything else is dropped. Pages render these through Jinja's autoescaping, never |safe.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

#: The Affiliates tab's list before it was a setting (2026-10-15): an outside Doginals index was one until
#: the operator removed it the same day, with its API. Explorer is where this arcade reads Doginals from (arcade/doginals.py).
SEED = (
    {"url": "https://antidoginaldogs.com", "name": "Anti Doginal Dogs",
     "about": "Recursive transformative art on Dogecoin.", "share": False},
)

MAX_SITES = 20          # in one "me" frame, and the operator's own added sites
MAX_URL = 200
MAX_NAME = 60
MAX_ABOUT = 200
MAX_PEERS = 200         # arcades whose word is kept
HEARD_DAYS = 30         # a ticked site from another arcade shows only while it was heard this recently
KEEP_DAYS = 90          # and is forgotten after this long unheard
REWRITE_EVERY = 3600    # an unchanged "me" refreshes the stored `at` at most this often (settings.json is not a log)

#: One DNS label, and a whole host of two or more of them ending in a letter TLD.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS = re.compile(rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]$")
#: Nothing a link or a card has any business holding: control characters, whitespace inside a URL, quotes, angle
#: brackets, backslashes and backticks.
_BAD_URL = re.compile(r"[\x00-\x20\x7f-\x9f\"'<>\\`]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


def clean_url(value: Any) -> str:
    """An affiliate link, or "" when it is not one. See the module's docstring for the shape."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL or _BAD_URL.search(value):
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or port is not None or parts.username is not None \
            or parts.password is not None or "@" in parts.netloc:
        return ""
    host = (parts.hostname or "").lower()
    if not host or host != parts.netloc.lower() or not _DNS.match(host) or len(host) > 253:
        return ""
    if all(label.isdigit() for label in host.split(".")):      # 10.0.0.1 shaped (the TLD rule refuses it anyway)
        return ""
    return urllib.parse.urlunsplit(("https", host, parts.path or "", parts.query, parts.fragment))


def clean_text(value: Any, most: int, least: int = 0) -> str | None:
    """Plain text of `least`..`most` characters, spaces run together; None when it is not."""
    if not isinstance(value, str) or len(value) > most * 2 + 8 or _CONTROL.search(value.replace("\t", " ")):
        return None
    text = " ".join(value.split())
    return text if least <= len(text) <= most else None


def clean_site(value: Any) -> dict | None:
    """{url, name, about} from anybody (a linked arcade, the Admin page), or None when any part is wrong."""
    if not isinstance(value, dict):
        return None
    url = clean_url(value.get("url"))
    name = clean_text(value.get("name"), MAX_NAME, 1)
    about = clean_text(value.get("about") if value.get("about") is not None else "", MAX_ABOUT)
    if not url or name is None or about is None:
        return None
    return {"url": url, "name": name, "about": about}


def clean_sites(value: Any) -> list[dict] | None:
    """A peer's `sites`: None when the field is not a list of at most MAX_SITES (left as it was), else the valid ones,
    each URL once. Counted before anything in it is looked at."""
    if not isinstance(value, list) or len(value) > MAX_SITES:
        return None
    out, seen = [], set()
    for item in value:
        site = clean_site(item)
        if site and site["url"] not in seen:
            seen.add(site["url"])
            out.append(site)
    return out


# ---------------------------------------------------------------- this arcade's own


def mine(state) -> list[dict]:
    """The operator's own added sites, {url, name, about, share}; SEED until the operator saves a list (an empty list
    saved stays empty)."""
    said = state.setting("affiliates_mine")
    if not isinstance(said, list):
        return [dict(s) for s in SEED]
    out = []
    for item in said:
        site = clean_site(item)
        if site:
            out.append({**site, "share": bool(item.get("share"))})
    return out


def host_sites(state) -> list[dict]:
    """https://<host> for each name the arcade is published under, with whether it is shared."""
    try:
        from .web import brand as brandlib
        name = brandlib.current().name
    except Exception:                                      # noqa: BLE001
        name = "Arcade"
    shared = set(state.setting("affiliates_hosts_shared", []) or [])
    out, seen = [], set()
    for host in getattr(state, "public_hosts", ()) or ():
        url = clean_url(f"https://{host}")
        if url and url not in seen:
            seen.add(url)
            out.append({"url": url, "name": (name or "Arcade")[:MAX_NAME],
                        "about": "An arcade: games, tokens and trades on the chain.", "share": url in shared})
    return out


def to_share(state) -> list[dict]:
    """What this arcade says it shares (the "me" frame's `sites`): its ticked names, then its ticked added sites, at
    most MAX_SITES. Always a list, so an operator unticking the last one tells the others to drop them."""
    out, seen = [], set()
    for site in host_sites(state) + mine(state):
        if site["share"] and site["url"] not in seen:
            seen.add(site["url"])
            out.append({k: site[k] for k in ("url", "name", "about")})
    return out[:MAX_SITES]


# ---------------------------------------------------------------- what others share


def heard(state) -> dict:
    said = state.setting("affiliates_heard", {})
    return said if isinstance(said, dict) else {}


def record(state, peer: str, tag: str, sites: list[dict] | None, now: float | None = None) -> bool:
    """Keep what a linked arcade said it shares (sites None: an older arcade, or a field that was not a list: nothing
    changes). An empty list drops the peer. Written only when something changed, or at most once per REWRITE_EVERY to
    refresh when it was heard. True when the setting was written."""
    if sites is None or not isinstance(peer, str) or not peer:
        return False
    now = time.time() if now is None else now
    book = heard(state)
    was = book.get(peer)
    if not sites:
        if was is None:
            return False
        book.pop(peer, None)
    else:
        if isinstance(was, dict) and was.get("sites") == sites and was.get("tag") == tag \
                and now - float(was.get("at") or 0) < REWRITE_EVERY:
            return False
        book[peer] = {"at": now, "tag": tag, "sites": sites}
    cutoff = now - KEEP_DAYS * 86400
    book = {p: v for p, v in book.items() if isinstance(v, dict) and float(v.get("at") or 0) >= cutoff}
    if len(book) > MAX_PEERS:
        book = dict(sorted(book.items(), key=lambda kv: -float(kv[1].get("at") or 0))[:MAX_PEERS])
    state.set_setting("affiliates_heard", book)
    return True


def shown(state) -> set[str]:
    said = state.setting("affiliates_shown", [])
    return {u for u in said if isinstance(u, str)} if isinstance(said, list) else set()


def others(state, live: dict | None = None) -> list[dict]:
    """Every arcade that shares sites, newest first: {peer, node, tag, at, sites: [{url, name, about, shown}]}.
    `live` is the operators share's peers (mesh id -> what it said, `at`), for a fresher tag and time."""
    live = live or {}
    ticked = shown(state)
    rows = []
    for peer, said in heard(state).items():
        if not isinstance(said, dict):
            continue
        now_said = live.get(peer) or {}
        at = max(float(said.get("at") or 0), float(now_said.get("at") or 0))
        tag = now_said.get("tag") or said.get("tag") or ""
        sites = [s for s in (clean_site(x) for x in (said.get("sites") or [])) if s]
        rows.append({"peer": peer, "node": peer[:12], "tag": tag, "at": at,
                     "sites": [{**s, "shown": s["url"] in ticked} for s in sites]})
    return sorted(rows, key=lambda r: -r["at"])


def for_tab(state, now: float | None = None) -> dict:
    """The client Affiliates tab: the operator's own added sites, then others' ticked ones grouped by who shares them
    (heard within HEARD_DAYS, still shared, each URL once and never one of the operator's own)."""
    now = time.time() if now is None else now
    own = [{k: s[k] for k in ("url", "name", "about")} for s in mine(state)]
    seen = {s["url"] for s in own}
    groups = []
    for row in others(state):
        if now - row["at"] > HEARD_DAYS * 86400:
            continue
        sites = []
        for s in row["sites"]:
            if s["shown"] and s["url"] not in seen:
                seen.add(s["url"])
                sites.append({k: s[k] for k in ("url", "name", "about")})
        if sites:
            groups.append({"by": f"@{row['tag']}" if row["tag"] else f"an arcade {row['node']}", "sites": sites})
    return {"own": own, "groups": groups}


def save(state, said: dict) -> None:
    """The Admin page's save: any of `mine` [{url, name, about, share}], `hosts_shared` [url], `shown` [url].
    Raises ValueError naming the first thing wrong; nothing is written then."""
    writes = {}
    if "mine" in said:
        items = said["mine"]
        if not isinstance(items, list) or len(items) > MAX_SITES:
            raise ValueError(f"at most {MAX_SITES} sites of your own")
        out, seen = [], set()
        for n, item in enumerate(items, 1):
            site = clean_site(item)
            if site is None:
                raise ValueError(f"site {n}: a name (1-{MAX_NAME} characters), an https:// address of a named site "
                                 f"(at most {MAX_URL} characters, no user name, port or IP address) and one line "
                                 f"about it (at most {MAX_ABOUT})")
            if site["url"] in seen:
                raise ValueError(f"site {n}: {site['url']} is listed twice")
            seen.add(site["url"])
            out.append({**site, "share": bool(item.get("share"))})
        writes["affiliates_mine"] = out
    if "hosts_shared" in said:
        urls = said["hosts_shared"]
        own = {s["url"] for s in host_sites(state)}
        if not isinstance(urls, list):
            raise ValueError("hosts_shared is a list of this arcade's names")
        writes["affiliates_hosts_shared"] = sorted({u for u in urls if isinstance(u, str) and u in own})
    if "shown" in said:
        urls = said["shown"]
        if not isinstance(urls, list) or len(urls) > MAX_PEERS * MAX_SITES:
            raise ValueError("shown is a list of addresses")
        writes["affiliates_shown"] = sorted({clean_url(u) for u in urls} - {""})
    for key, value in writes.items():
        state.set_setting(key, value)
