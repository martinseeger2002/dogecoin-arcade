"""Content screening: this arcade asks a model about content before it shows it.

2026-09-25 (a friend's idea): before this instance serves any content --
an image inscription, or words on the feed -- it runs them past a model. Obscene
material goes behind a cover the viewer can lift; anything illegal is not shown
and cannot be uncovered.

**It is this instance's choice, not the protocol's.** The chain holds whatever it
holds and nothing here changes that; this decides what THIS node draws. An
operator turns it on with a `moderation` setting naming any OpenAI-compatible
chat endpoint that reads images (the operator's: Qwen3.8-Flash-Next on a test machine). No
setting, no screening, and the node behaves exactly as it did before.

**Verdicts** are "ok", "sensitive" (nudity, sexual content, graphic violence or
gore: legal for adults, behind a click-to-show cover) and "illegal" (sexual
content involving anyone who may be a minor, or other content that is illegal to
show: removed, no way to uncover it). They are kept per CONTENT -- the sha256 of
the bytes or the words -- so a picture or a sentence is judged once however many
times it is posted, shared or copied.

**When the model cannot be asked**, nothing is shown as if it had been judged:
the content is "checking" and stays covered, and the next request asks again.

**An illegal verdict is logged** for the operator (`moderation-illegal.log`), who
may have duties of their own -- reporting child sexual abuse material to NCMEC,
in the US. This module hides it; it cannot decide those for anybody.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OK, SENSITIVE, ILLEGAL = "ok", "sensitive", "illegal"
VERDICTS = (OK, SENSITIVE, ILLEGAL)

PROMPT = (
    'You review content for a public website before it is shown. Classify it into exactly '
    'one verdict: "ok" (fine for everyone), "sensitive" (nudity, sexual content, graphic '
    'violence or gore; legal for adults but should be behind a click-to-show cover), or '
    '"illegal" (sexual content involving anyone who may be a minor, or other content that '
    'is illegal to show). Answer only with JSON: '
    '{"verdict": "ok"|"sensitive"|"illegal", "reason": "<a few words>"}')

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict (
    digest     TEXT PRIMARY KEY,   -- sha256 of the bytes or the words
    kind       TEXT NOT NULL,      -- image | text
    verdict    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    checked_at INTEGER NOT NULL
);
"""

#: Longest side of an image as the model is shown it: enough to judge, quick to send.
LOOK_SIZE = 768
#: How long one question may take before the content is left "checking".
TIMEOUT = 60


def digest_of(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


class Screen:
    """The verdicts this node keeps, and the model it asks for new ones."""

    def __init__(self, home: Path, config: Any = None):
        self.config = config if isinstance(config, dict) else None
        self.enabled = bool(self.config and self.config.get("url") and self.config.get("model"))
        self.home = Path(home)
        self.conn = sqlite3.connect(self.home / "moderation.sqlite", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._queue: dict[str, str] = {}          # digest -> words waiting for a verdict
        self._worker: threading.Thread | None = None

    # --- what is already known -------------------------------------------------

    def known(self, digest: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT verdict FROM verdict WHERE digest = ?",
                                    (digest,)).fetchone()
        return row["verdict"] if row else None

    def _keep(self, digest: str, kind: str, verdict: str, reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO verdict (digest, kind, verdict, reason, model, checked_at)"
                " VALUES (?,?,?,?,?,?)",
                (digest, kind, verdict, reason[:200],
                 str((self.config or {}).get("model", "")), int(time.time())))
            self.conn.commit()
        if verdict == ILLEGAL:
            try:
                with open(self.home / "moderation-illegal.log", "a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{digest}\t{reason}\n")
            except OSError:
                pass
            log.warning("content %s judged illegal (%s): hidden, logged for the operator",
                        digest[:16], reason)

    # --- asking ------------------------------------------------------------------

    def _ask(self, content: Any) -> tuple[str, str] | None:
        """One question to the model; None if it could not be asked or answered."""
        cfg = self.config or {}
        body = {"model": cfg["model"], "temperature": 0, "max_tokens": 80,
                "messages": [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": content}],
                "chat_template_kwargs": {"enable_thinking": False}}
        headers = {"Content-Type": "application/json"}
        if cfg.get("key"):
            headers["Authorization"] = f"Bearer {cfg['key']}"
        url = str(cfg["url"]).rstrip("/") + "/chat/completions"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=json.dumps(body).encode(), headers=headers),
                    timeout=float(cfg.get("timeout", TIMEOUT))) as answer:
                said = json.load(answer)["choices"][0]["message"]["content"]
        except Exception as exc:                      # noqa: BLE001 -- the model is away
            log.info("screening could not ask %s: %s", url, exc)
            return None
        found = re.search(r"\{.*\}", said or "", re.S)
        try:
            parsed = json.loads(found.group(0)) if found else {}
        except ValueError:
            parsed = {}
        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in VERDICTS:
            return None
        return verdict, str(parsed.get("reason", ""))

    def check_image(self, content_type: str, body: bytes) -> str | None:
        """The verdict for an image, asking now if it is not known. None = checking."""
        digest = digest_of(body)
        known = self.known(digest)
        if known or not self.enabled:
            return known
        looked = _for_the_model(content_type, body)
        if looked is None:                            # not a picture a model can see
            return self.check_text(body.decode("utf-8", "replace")[:20000], now=True)
        said = self._ask([{"type": "text", "text": "An image someone published:"},
                          {"type": "image_url", "image_url": {"url": looked}}])
        if said is None:
            return None
        self._keep(digest, "image", *said)
        return said[0]

    def check_text(self, words: str, now: bool = False) -> str | None:
        """The verdict for some words. Known -> it; otherwise queued for the worker
        (or asked at once with `now`), and None meanwhile = checking."""
        words = (words or "").strip()
        if not words:
            return OK
        digest = digest_of(words)
        known = self.known(digest)
        if known or not self.enabled:
            return known
        if now:
            said = self._ask("Text someone published:\n\n" + words[:8000])
            if said is None:
                return None
            self._keep(digest, "text", *said)
            return said[0]
        with self._lock:
            self._queue[digest] = words
        self._wake()
        return None

    # --- the background worker for words -------------------------------------------

    def _wake(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain, name="screen", daemon=True)
        self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                digest, words = next(iter(self._queue.items()))
            said = self._ask("Text someone published:\n\n" + words[:8000])
            with self._lock:
                self._queue.pop(digest, None)
            if said is not None:
                self._keep(digest, "text", *said)
            else:
                time.sleep(5)                          # the model is away: try again later


# --- pictures ----------------------------------------------------------------------

def _for_the_model(content_type: str, body: bytes) -> str | None:
    """An image as a data: URL the model can read, made small; None if it is not one."""
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(body)) as im:
            im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((LOOK_SIZE, LOOK_SIZE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:                                 # noqa: BLE001 -- not a picture after all
        return None


def blurred(body: bytes) -> bytes | None:
    """The cover a sensitive picture wears: the picture, blurred past recognising."""
    try:
        from PIL import Image, ImageFilter
        with Image.open(io.BytesIO(body)) as im:
            im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((96, 96))                    # small first: nothing survives
            im = im.filter(ImageFilter.GaussianBlur(radius=max(4, max(im.size) // 8)))
            im = im.resize((max(1, im.width * 4), max(1, im.height * 4)))
            out = io.BytesIO()
            im.save(out, "PNG")
        return out.getvalue()
    except Exception:                                 # noqa: BLE001
        return None


def notice_image(text: str) -> bytes:
    """A plain card with a few words on it: "Removed by this arcade", "Checking…"."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (480, 300), (40, 38, 34))
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:                                  # an older Pillow: no sizes
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((480 - (box[2] - box[0])) // 2, (300 - (box[3] - box[1])) // 2), text,
              fill=(236, 232, 224), font=font)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()
