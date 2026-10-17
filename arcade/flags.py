"""Content flags shared between arcades: one operator's screening, offered to every other arcade.

2026-10-17: an arcade whose content screening is on (arcade/moderation.py) may
broadcast the "sensitive" and "illegal" verdicts its model reaches, and every other arcade
may choose, operator by operator, to use them. They travel as public machine talk
(messaging/broadcast.py) under the topic "flags/v1", from the node's fee address:

    {"f": [[sha256 of the content, "sensitive" | "illegal" | "ok"], ...]}

The digest is the one moderation keys its verdicts by -- the same for the same picture or
words wherever they are posted -- and "ok" takes back a flag the sender gave before. Many
flags share one transaction (PER_TX), gathered for BATCH seconds.

**Nobody is bound by anybody.** Flags never change what is on the chain, and an arcade
that follows nobody draws exactly what it drew before. Following can only make content
stricter, and the arcade's own operator keeps the last word on their node
(moderation.Screen.overrule).

**Only the model's own verdicts go out.** A verdict this arcade took from somebody else's
flags is not re-broadcast, so two arcades that follow each other never echo.

Settings (state.setting):
    flags:broadcast   bool, off by default: publish this arcade's own verdicts
    flags:follow      list of fee addresses whose flags this arcade uses
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

TOPIC = "flags/v1"
OK, SENSITIVE, ILLEGAL = "ok", "sensitive", "illegal"
SEVERITY = {OK: 0, SENSITIVE: 1, ILLEGAL: 2}
DIGEST = re.compile(r"^[0-9a-f]{64}$")

#: Flags in one broadcast: ~80 bytes each, inside broadcast.MAX_BYTES with room to spare.
PER_TX = 70
#: How long the first new flag waits for others to share its transaction.
BATCH = 30.0
#: At most this many broadcasts a day, so a flood of flagged posts never empties the coins.
PER_DAY = 200
#: After a failed send (no coins, node down) the sender waits this long and tries again.
RETRY = 60.0


def worse(a: str | None, b: str | None) -> str | None:
    """The stricter of two verdicts; None is "not known"."""
    if a is None:
        return b
    if b is None:
        return a
    return a if SEVERITY.get(a, 0) >= SEVERITY.get(b, 0) else b


def body(entries: list[tuple[str, str]]) -> dict:
    return {"f": [[d, v] for d, v in entries]}


def entries_of(raw) -> list[tuple[str, str]]:
    """The (digest, verdict) pairs a stored broadcast body holds; nothing malformed."""
    try:
        said = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        items = said.get("f") if isinstance(said, dict) else None
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                and DIGEST.match(item[0]) and item[1] in SEVERITY):
            out.append((item[0], item[1]))
    return out


def latest_by(store, network: str, senders: list[str]) -> dict[tuple[str, str], str]:
    """(sender, digest) -> the latest verdict that sender gave that content."""
    out: dict[tuple[str, str], str] = {}
    after = 0
    while True:
        rows = store.node_broadcasts(network, TOPIC, after=after, limit=1000, senders=senders)
        if not rows:
            return out
        # node_broadcasts is in row order, which is the order they were read; a row
        # this node wrote at publish (height 0) is its newest word either way.
        for row in rows:
            for digest, verdict in entries_of(row["body"]):
                out[(row["sender"], digest)] = verdict
        after = rows[-1]["id"]


def followed_verdicts(store, network: str, senders: list[str]) -> dict[str, str]:
    """digest -> the strictest standing flag among the followed senders."""
    out: dict[str, str] = {}
    for (_sender, digest), verdict in latest_by(store, network, senders).items():
        if verdict != OK:
            out[digest] = worse(out.get(digest), verdict)
    return out


def broadcasters(store, network: str) -> list[dict]:
    """Every address that has broadcast flags on this chain (confirmed), newest first."""
    seen: dict[str, dict] = {}
    after = 0
    while True:
        rows = store.node_broadcasts(network, TOPIC, after=after, limit=1000)
        if not rows:
            break
        for row in rows:
            if int(row["height"] or 0) <= 0:
                continue
            got = seen.setdefault(row["sender"], {"address": row["sender"], "broadcasts": 0,
                                                  "sensitive": 0, "illegal": 0,
                                                  "retracted": 0, "last": 0})
            got["broadcasts"] += 1
            got["last"] = max(got["last"], int(row["block_time"] or 0))
            for _digest, verdict in entries_of(row["body"]):
                got[{SENSITIVE: "sensitive", ILLEGAL: "illegal", OK: "retracted"}[verdict]] += 1
        after = rows[-1]["id"]
    return sorted(seen.values(), key=lambda b: -b["last"])


class Broadcaster:
    """Publishes this arcade's own verdicts in batches, in the background.

    `publish(entries)` puts one broadcast on the chain (state.send_broadcast) and returns
    its txid; `said()` is digest -> verdict as this arcade's address last said it, so
    nothing is said twice and an "ok" goes out only to take back a flag that went out.
    """

    def __init__(self, publish: Callable[[list[tuple[str, str]]], str],
                 said: Callable[[], dict[str, str]], batch: float = BATCH,
                 per_day: int = PER_DAY, retry: float = RETRY):
        self.publish, self.said = publish, said
        self.batch, self.per_day, self.retry = batch, per_day, retry
        self._queue: dict[str, str] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._sent: list[float] = []
        self.last_error = ""
        self.last_sent = 0.0
        self.flags_sent = 0

    def offer(self, digest: str, verdict: str) -> bool:
        """Queue one verdict, unless it would say nothing new."""
        if verdict not in SEVERITY or not DIGEST.match(digest or ""):
            return False
        try:
            before = self.said().get(digest)
        except Exception:                                 # noqa: BLE001 -- checked again at send
            before = None
        if before == verdict or (verdict == OK and before in (None, OK)):
            return False
        with self._lock:
            self._queue[digest] = verdict
        self._wake()
        return True

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            queued = len(self._queue)
        return {"queued": queued, "flags_sent": self.flags_sent,
                "broadcasts_today": len([t for t in self._sent if now - t < 86400]),
                "per_day": self.per_day, "last_error": self.last_error,
                "last_sent": int(self.last_sent)}

    def _wake(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain, name="flags", daemon=True)
        self._worker.start()

    def _drain(self) -> None:
        time.sleep(self.batch)                            # let a burst share one transaction
        while True:
            with self._lock:
                if not self._queue:
                    return
                waiting = list(self._queue.items())
            now = time.time()
            self._sent = [t for t in self._sent if now - t < 86400]
            if len(self._sent) >= self.per_day:
                self.last_error = f"{self.per_day} broadcasts in the last day: the rest wait"
                time.sleep(self.retry)
                continue
            try:
                said = self.said()
                already = [(d, v) for d, v in waiting if said.get(d) == v]
                fresh = [(d, v) for d, v in waiting if said.get(d) != v][:PER_TX]
                if fresh:
                    txid = self.publish(fresh)
                    log.info("%d content flags broadcast in %s", len(fresh), txid)
                    self.last_sent = time.time()
                    self._sent.append(self.last_sent)
                    self.flags_sent += len(fresh)
                self.last_error = ""
                done = dict(already + fresh)
                with self._lock:
                    for digest, verdict in done.items():
                        if self._queue.get(digest) == verdict:
                            self._queue.pop(digest, None)
            except Exception as exc:                      # noqa: BLE001 -- kept, tried again
                self.last_error = str(exc)[:200]
                log.warning("content flags not broadcast yet: %s", exc)
                time.sleep(self.retry)
