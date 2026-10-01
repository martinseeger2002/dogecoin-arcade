"""Games: inscribed pages that say they are one, listed on the Games tab.

2026-09-30: "the game Builder could inscribe an HTML file referencing
to their games that has something in json that indexes it as a game. The Games
place list in the Games tab can have the same sorting as collections and the
same ability for people to comment like or tip."

So a game is an inscription that is a page (text/html) and whose JSON carries
a `game` object. Nothing new goes on the chain and nobody registers anything:
the index already holds every inscription's JSON, and this reads it. The page
is what a player opens to play -- the whole game, or a small launcher that
loads its parts from other inscriptions.

    {"game": {"name": "ASHVALE",                       required, 1-60 characters
              "description": "A small valley RPG.",    up to 280
              "version": "1.2",                        up to 20
              "players": "1-50",                       up to 20, said as the maker likes
              "multiplayer": true,                     plays with others (arcade.realtime)
              "family": "ashvale",                     what its versions share (rooms too)
              "cover": "<inscription txid>",           a picture for the card
              "genre": "rpg"}}                         up to 24

**One card per game.** The newest inscription per creator and name is the
game; inscribing a new version with the same name replaces the card, and
the likes and comments of the old one stay with the old one, as a mintpad's
do. Only the creator can replace a game, because the key is the creator's
address -- somebody else's page with the same name is somebody else's game.

**Read, never trusted.** Everything here is somebody's words on the chain, so
each field is cut to its length and anything malformed is simply absent: a
`game` that is not an object, or has no usable name, is not a game.
"""

from __future__ import annotations

import json
import re
from typing import Any

MARK = "game"
_TXID = re.compile(r"^[0-9a-f]{64}$")
_FAMILY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
LIMITS = {"name": 60, "description": 280, "version": 20, "players": 20, "genre": 24}


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def parse(json_text: Any) -> dict | None:
    """The `game` an inscription's JSON declares, cleaned, or None."""
    if isinstance(json_text, (bytes, bytearray)):
        json_text = json_text.decode("utf-8", "replace")
    try:
        said = json.loads(json_text) if isinstance(json_text, str) else json_text
    except ValueError:
        return None
    if not isinstance(said, dict) or not isinstance(said.get(MARK), dict):
        return None
    game = said[MARK]
    out = {key: _text(game.get(key), limit) for key, limit in LIMITS.items()}
    if not out["name"]:
        return None
    out["multiplayer"] = game.get("multiplayer") is True
    family = game.get("family")
    out["family"] = family if isinstance(family, str) and _FAMILY.match(family) else ""
    cover = str(game.get("cover") or "").lower()
    out["cover"] = cover if _TXID.match(cover) else ""
    return out


def is_page(content_type: Any) -> bool:
    return str(content_type or "").lower().startswith("text/html")


def newest_per_game(rows: list[dict]) -> list[dict]:
    """Rows newest first in, one per (creator, name) out: a re-inscribed game
    replaces its older self. Each row needs `creator` and a parsed `game`."""
    seen, out = set(), []
    for row in rows:
        key = (row["creator"], row["game"]["name"].casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
