"""The DogecoinArcade expert in the Exchange's chat (2026-10-23: "When they are chatting with the model in browser
that model should include a system prompt that it is an expert on Dogecoin arcade and it should have access to all of
the Dogecoin arcade documentation"). His choice: the parts of the docs that bear on the question, not all of them, and
those added words are not charged to the buyer.

The selling arcade adds them itself (llm.Seller.chat, `docs: true`), so what goes free is what it put there, never what
a buyer's arcade claims. Only the PUBLIC documentation (the /docs a stranger may read): never the plans and the
engineering history (guide.PRIVATE), nor the hosting plan (operator-only, publish.sh PRIVATE).

Choosing the parts: every document is cut at its headings into sections of at most SECTION characters, and the
question (with the person's last few messages) is scored against each by BM25 over plain words. The best sections go in,
up to BUDGET characters, each under its document's title and heading so the model can say where an answer is from.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter

SECTION = 2400
BUDGET = 24000                 # characters of documentation per question (~6-8k tokens)
KEEP_FOR = 300.0               # the cut documents are read again after this many seconds
LEAVE_OUT = ("hosting-plan.md",)
WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{1,}")
STOP = frozenset("""the a an and or of to in on for is are be it this that with as by at from your you can how what
which who when where why do does not no if then than so it's its into about there their them they we our us i me my""".split())

PROMPT = """You are the DogecoinArcade assistant, an expert on DogecoinArcade: the node and web app for on-chain games,
inscriptions, tokens (Omni, DRC-20, PRC-20), swaps, prize pools, the mesh between arcades, the LLM service and everything
else in its documentation. Answer questions from players, game makers and arcade operators clearly and concretely.
Base your answers on the documentation below; when it does not cover something, say so plainly rather than guess.
Name the page an answer comes from when it helps (the documentation lives at /docs on every arcade). Keep answers short
unless the person asks for detail. Never ask for or accept anyone's password, recovery words or private keys."""

_lock = threading.Lock()
_cache: tuple[float, list[dict], dict, float] | None = None


def _words(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


def _cut(name: str, title: str, text: str) -> list[dict]:
    out, head, buf = [], title, []

    def flush():
        body = "\n".join(buf).strip()
        while body:
            piece, body = body[:SECTION], body[SECTION:]
            if len(piece.strip()) > 40:
                out.append({"doc": name, "title": title, "head": head, "text": piece.strip()})
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s", line):
            flush()
            buf = []
            head = line.lstrip("#").strip() or title
        else:
            buf.append(line)
    flush()
    return out


def sections() -> tuple[list[dict], dict, float]:
    """Every public section, its words counted, and the corpus figures BM25 needs (kept KEEP_FOR seconds)."""
    global _cache
    with _lock:
        if _cache and time.time() - _cache[0] < KEEP_FOR:
            return _cache[1], _cache[2], _cache[3]
        from .web import guide
        secs: list[dict] = []
        for p in guide.pages(private=False):
            if p["name"] in LEAVE_OUT:
                continue
            text = guide.page(p["name"]) or ""
            secs += _cut(p["name"], p["title"], text)
        df: Counter = Counter()
        for s in secs:
            s["tf"] = Counter(_words(s["head"] + " " + s["head"] + " " + s["text"]))
            s["len"] = sum(s["tf"].values()) or 1
            df.update(s["tf"].keys())
        avg = sum(s["len"] for s in secs) / max(1, len(secs))
        _cache = (time.time(), secs, dict(df), avg)
        return secs, dict(df), avg


def pick(question: str, budget: int = BUDGET) -> list[dict]:
    """The sections that bear most on `question`, best first, up to `budget` characters."""
    secs, df, avg = sections()
    q = _words(question)
    if not q or not secs:
        return []
    n, k1, b = len(secs), 1.4, 0.75
    scored = []
    for s in secs:
        score = 0.0
        for w in set(q):
            f = s["tf"].get(w)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * s["len"] / avg))
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    out, used = [], 0
    for _, s in scored:
        if used + len(s["text"]) > budget:
            continue
        out.append(s)
        used += len(s["text"])
        if used > budget * 0.9:
            break
    return out


def system_message(messages: list, arcade: str = "DogecoinArcade", page: str = "") -> str:
    """The system prompt for a chat: who the model is, where the person is in the arcade (the helper says it: the operator
    2026-10-23 "should have context on what tab the user is on"), then the sections that bear on what they asked."""
    asked = [str(m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
             and isinstance(m.get("content"), str)]
    question = " ".join(asked[-3:])[-4000:]
    parts = pick(question)
    if arcade and arcade != "DogecoinArcade":
        head = PROMPT + f"\nThis arcade is {arcade}, a DogecoinArcade node."
    else:
        head = PROMPT
    page = re.sub(r"[\x00-\x1f]+", " ", str(page or ""))[:600].strip()
    if page:
        head += ("\n\nWhere the person is right now in the arcade (use it to understand questions like \"what is this\" "
                 "or \"how do I do this here\"): " + page)
        question = (question + " " + page)[-4600:]
        parts = pick(question)
    if not parts:
        return head + "\n\n(No section of the documentation matched this question.)"
    docs = "\n\n".join(f"### {p['title']} -- {p['head']} (/docs/{p['doc']})\n{p['text']}" for p in parts)
    return head + "\n\n# DogecoinArcade documentation (the parts that bear on this question)\n\n" + docs
