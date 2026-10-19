"""Card watch: game cards that pose as somebody else's game are hidden until the operator looks (2026-10-18).

The operator: "somebody can make a clone of Ashvale it could be spelt slightly different with two L's ... People could
unbeknownst to them click on it, and it could drain their wallet", and "have our local model watch card
publications, and flag any that appear as duplications from a invalid user" -- "Hide until reviewed by operator.
Send operator node account a notification."

Every new game card (games.py) is checked against every other game on the Games tab by a DIFFERENT creator:

1. **A cheap look** (no model): the names and families brought to one spelling -- case, accents, lookalike letters
   and digits (0/o, 1/l/i, 5/s, Cyrillic a/e/o ...), doubled letters (ASHVALLE = ASHVALE), spaces and punctuation --
   then equal, one inside the other, or two edits apart; or the same cover picture. Nothing close: the card is ok.
2. **The model** (any OpenAI-compatible chat endpoint; by default the one content screening uses, setting
   `moderation`, or `cardwatch_model` of its own): both cards' names, descriptions, genres and publishers, asked
   whether the new one looks made to be mistaken for the other. Without a model, a close look is a flag on its own.

**Who is the real one.** Of two lookalike games, the one inscribed first, unless the operator has marked a
publisher trusted (setting `cardwatch_trusted`, addresses): a trusted publisher's cards are never flagged, and
anybody else's lookalike of them is. A creator's own new versions are never compared with its own games.

**Flagged** cards leave the Games tab and their /play links until the operator clears them (Admin, Game cards);
"confirmed" keeps them hidden for good. The operator account gets a notification for each flag. The inscription
stays on the chain and plays from its own link; this only decides what THIS node lists.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("arcade.cardwatch")

OK, FLAGGED, CLEARED, CONFIRMED = "ok", "flagged", "cleared", "confirmed"
HIDDEN = (FLAGGED, CONFIRMED)
EVERY = 60.0                  # seconds between looks for new cards
TIMEOUT = 60.0

# Lookalikes brought to one letter: digits and symbols people use for letters, and the Cyrillic and Greek letters
# that look Latin.
_LOOK = str.maketrans({
    "0": "o", "1": "l", "!": "l", "|": "l", "3": "e", "4": "a", "@": "a", "5": "s", "$": "s", "7": "t", "8": "b",
    "9": "g", "6": "g", "2": "z", "i": "l", "j": "l",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m", "т": "t", "н": "h",
    "в": "b", "і": "l", "ј": "l", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ν": "v", "ο": "o", "α": "a", "ε": "e", "ι": "l",
    "κ": "k", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
})


def spelling(name: Any) -> str:
    """A name brought to one spelling: lower case, no accents, lookalikes as one letter, only letters and digits,
    and no letter twice in a row."""
    text = unicodedata.normalize("NFKD", str(name or "")).lower()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("rn", "m").replace("vv", "w").replace("cl", "d")
    text = text.translate(_LOOK)
    text = re.sub(r"[^a-z0-9]", "", text)
    return re.sub(r"(.)\1+", r"\1", text)


def _edits(a: str, b: str, most: int) -> int:
    """Levenshtein distance, given up past `most`."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > most:
            return most + 1
        prev = cur
    return prev[-1]


def looks_like(new: dict, old: dict) -> str:
    """Why the new card looks like the old one, or '' if it does not. Cards are {name, family, cover}."""
    a, b = spelling(new.get("name")), spelling(old.get("name"))
    fa, fb = spelling(new.get("family")), spelling(old.get("family"))
    if new.get("cover") and new.get("cover") == old.get("cover"):
        return "the same cover picture"
    for x, y, what in ((a, b, "name"), (fa, fb, "family"), (a, fb, "name"), (fa, b, "family")):
        if not x or not y or len(min(x, y, key=len)) < 4:
            continue
        if x == y:
            return f"the same {what} once spelled plainly"
        if len(y) >= 5 and (y in x or x in y):
            return f"the {what} contains the other"
        if _edits(x, y, 2) <= (1 if len(y) < 7 else 2):
            return f"a {what} one or two letters away"
    return ""


PROMPT = (
    "You check new game listings on a game site for impersonation. A scammer copies a popular game's listing with a "
    "lookalike name (an extra letter, a digit for a letter, a lookalike character) so players open the copy and "
    "trust it with their wallet. You get an EXISTING game and a NEW listing by a DIFFERENT publisher. Answer only "
    'JSON: {"copy": true|false, "why": "<one short sentence>"}. copy is true when the new listing looks made to be '
    "mistaken for the existing one (name, wording, description), false when it is plainly its own game (a "
    "different game that happens to share a common word, a clearly different title).")


class CardWatch:
    def __init__(self, path: Path, model: Callable[[], dict | None] = lambda: None,
                 trusted: Callable[[], set] = lambda: set()):
        self.path = Path(path)
        self.model, self.trusted = model, trusted
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS card (seq INTEGER PRIMARY KEY AUTOINCREMENT, txid TEXT UNIQUE, "
                       "net TEXT, creator TEXT, name TEXT, status TEXT, like_txid TEXT, like_name TEXT, "
                       "like_creator TEXT, why TEXT, by TEXT, at INTEGER)")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def status(self) -> dict[str, str]:
        with self._db() as db:
            return {r["txid"]: r["status"] for r in db.execute("SELECT txid, status FROM card")}

    def hidden(self) -> set[str]:
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT txid FROM card WHERE status IN (?, ?)", HIDDEN)}

    def rows(self, statuses=(FLAGGED, CONFIRMED, CLEARED), limit: int = 200) -> list[dict]:
        q = ",".join("?" * len(statuses))
        with self._db() as db:
            return [dict(r) for r in db.execute(f"SELECT * FROM card WHERE status IN ({q}) ORDER BY seq DESC LIMIT ?",
                                                (*statuses, limit))]

    def decide(self, txid: str, status: str) -> bool:
        if status not in (CLEARED, CONFIRMED, FLAGGED):
            raise ValueError("a decision is cleared, confirmed or flagged")
        with self._lock, self._db() as db:
            return db.execute("UPDATE card SET status=?, at=? WHERE txid=?", (status, int(time.time()), txid)).rowcount > 0

    def _ask(self, new: dict, old: dict) -> tuple[bool, str] | None:
        cfg = self.model() or {}
        if not cfg.get("url") or not cfg.get("model"):
            return None
        said = {k: {"name": c.get("name"), "description": c.get("description", ""), "genre": c.get("genre", ""),
                    "family": c.get("family", ""), "publisher": c.get("publisher", "")} for k, c in
                (("existing", old), ("new", new))}
        body = {"model": cfg["model"], "temperature": 0, "max_tokens": 120,
                "messages": [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": json.dumps(said, ensure_ascii=False)}],
                "chat_template_kwargs": {"enable_thinking": False}}
        headers = {"Content-Type": "application/json"}
        if cfg.get("key"):
            headers["Authorization"] = f"Bearer {cfg['key']}"
        try:
            with urllib.request.urlopen(urllib.request.Request(str(cfg["url"]).rstrip("/") + "/chat/completions",
                                                               data=json.dumps(body).encode(), headers=headers),
                                        timeout=float(cfg.get("timeout", TIMEOUT))) as r:
                text = json.load(r)["choices"][0]["message"]["content"]
            found = re.search(r"\{.*\}", text or "", re.S)
            parsed = json.loads(found.group(0)) if found else {}
            if not isinstance(parsed.get("copy"), bool):
                return None
            return parsed["copy"], str(parsed.get("why", ""))[:300]
        except Exception as exc:                          # noqa: BLE001 -- asked again on the next look
            log.info("card watch could not ask the model: %s", exc)
            return None

    def check(self, cards: list[dict]) -> list[dict]:
        """Look at every card not looked at yet. `cards`: [{txid, net, creator, name, family, cover, description,
        genre, publisher, height}], oldest first is not needed. Returns the newly flagged rows."""
        known = self.status()
        trusted = {str(t).strip() for t in (self.trusted() or set()) if t}
        order = sorted(cards, key=lambda c: (int(c.get("height") or 10 ** 9), c["txid"]))
        flagged = []
        for i, new in enumerate(order):
            if new["txid"] in known:
                continue
            verdict, like, why, by = OK, None, "", ""
            if new["creator"] not in trusted:
                for old in order:
                    if old["creator"] == new["creator"] or old["txid"] == new["txid"]:
                        continue
                    first = (int(old.get("height") or 10 ** 9), old["txid"]) < (int(new.get("height") or 10 ** 9), new["txid"])
                    if not (first or old["creator"] in trusted):
                        continue                  # the other one is the newcomer: it is judged in its own turn
                    if known.get(old["txid"]) in HIDDEN:
                        continue                  # never protect a copy
                    close = looks_like(new, old)
                    if not close:
                        continue
                    asked = self._ask(new, old)
                    if asked is None and self.model():
                        verdict = "later"         # the model is away: look again next time
                        break
                    if asked is None or asked[0]:
                        verdict, like = FLAGGED, old
                        why, by = (asked[1] if asked else "") or close, "model" if asked else "lookalike"
                        why = f"{close}: {why}" if asked and close not in why else why
                        break
            if verdict == "later":
                continue
            with self._lock, self._db() as db:
                db.execute("INSERT OR IGNORE INTO card (txid, net, creator, name, status, like_txid, like_name, "
                           "like_creator, why, by, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (new["txid"], new.get("net", ""), new["creator"], new.get("name", ""), verdict,
                            like["txid"] if like else None, like.get("name") if like else None,
                            like["creator"] if like else None, why, by, int(time.time())))
            known[new["txid"]] = verdict
            if verdict == FLAGGED:
                log.warning("card watch: hid %s (%r by %s): looks like %r by %s -- %s", new["txid"][:12],
                            new.get("name"), new["creator"], like.get("name"), like["creator"], why)
                flagged.append(new)
        return flagged
