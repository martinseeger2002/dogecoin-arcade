"""The written documentation, inside the application.

Somebody offline, or on a clone, or who does not know there is a website,
still has to be able to find out what the thing in front of them does. So
the documents ship WITH the application rather than only on a site: the
whole of `docs/` is copied into `web/docs/` at release time and checked by
a test, and every instance serves it -- which is also what lets a clone be
a complete copy rather than a program with its manual somewhere else.

There used to be a second, shorter guide written by hand in this module,
kept in step by a test that compared section titles. Two descriptions of
one program is one too many: the short one was always a little behind, and
the test only noticed when a heading changed, never when a sentence went
stale.

Markdown is rendered here rather than by a library. The subset these
documents use is small -- headings, bullets, emphasis, code, links, tables,
rules -- and a wallet should not grow a dependency to show its own help.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

GUIDE = Path(__file__).parent / "templates" / "guide.md"

#: Everything else, as shipped. One directory, read at request time like the
#: templates are, so a document can be corrected without a restart.
DOCS = Path(__file__).parent / "docs"

#: The order they are offered in: what somebody arriving wants first, then
#: what somebody building on it wants, then the history. A document not
#: named here is still served -- it goes at the end under its own file name
#: -- so adding one to `docs/` is enough to publish it.
ORDER = (
    ("features.md", "What it does", "Every part of the application, in one page."),
    ("inscription-api.md", "The inscription API",
     "What an inscribed page may ask the wallet, and what it is refused."),
    ("bot-rpc.md", "The bot RPC",
     "The JSON-RPC endpoint for programs running beside the wallet."),
    ("multi-user.md", "Letting other people in",
     "The plan for accounts, seats and keys that never leave the browser."),
    ("00-node-setup.md", "Setting up a node",
     "What the installer does, and how to do it by hand."),
    ("messaging/02-design.md", "Messaging: the design",
     "The envelope, the encryption and what the chain carries."),
    ("messaging/01-node.md", "Messaging: the node", "The testnet side of it."),
    ("messaging/04-testnet-results.md", "Messaging: measured",
     "What it actually cost and how long it actually took."),
    ("p2p-messaging.md", "Why not peer-to-peer",
     "The design that was rejected, and why."),
    ("tokens-notes.md", "Tokens: notes", "Working notes on the token layer."),
    ("03-companion-app-design.md", "The companion app", "An earlier design."),
    ("M0-notes.md", "Milestone 0", "Working notes."),
    ("M1-notes.md", "Milestone 1", "Working notes."),
    ("M2-notes.md", "Milestone 2", "Working notes."),
    ("DECISIONS.md", "Every decision, and why",
     "The engineering record: what was chosen, what was rejected, and what "
     "went wrong first. The longest document here and the most useful one "
     "if you are going to change something."),
)


def pages() -> list[dict]:
    """Every document that ships, in the order above and then the rest."""
    named = {name for name, _, _ in ORDER}
    found = []
    for name, title, blurb in ORDER:
        if (DOCS / name).exists() or name == "features.md":
            found.append({"name": name, "title": title, "blurb": blurb})
    for path in sorted(DOCS.rglob("*.md")):
        name = str(path.relative_to(DOCS))
        if name not in named:
            found.append({"name": name, "title": path.stem.replace("-", " "),
                          "blurb": ""})
    return found


def page(name: str) -> str | None:
    """One document's text, or None. Never a path somebody handed us."""
    if name == "features.md":
        return GUIDE.read_text(encoding="utf-8") if GUIDE.exists() else None
    if any(part in ("..", "") for part in Path(name).parts):
        return None
    target = (DOCS / name).resolve()
    if not str(target).startswith(str(DOCS.resolve())) or not target.is_file():
        return None
    if target.suffix != ".md":
        return None
    return target.read_text(encoding="utf-8")


def title_of(name: str) -> str:
    for known, title, _ in ORDER:
        if known == name:
            return title
    return Path(name).stem.replace("-", " ")


def document(text: str) -> list[dict]:
    """A whole document as (title, html) sections, one per `## ` heading."""
    found: list[dict] = [{"title": "", "body": []}]
    for line in (text or "").splitlines():
        if line.startswith("## "):
            found.append({"title": line[3:].strip(), "body": []})
        elif line.startswith("# "):
            continue                      # the document's own title
        else:
            found[-1]["body"].append(line)
    return [{"title": s["title"], "html": render(s["body"])}
            for s in found if any(line.strip() for line in s["body"])]

#: Where a bug goes. In the application because that is where somebody is
#: standing when they find one.
BUGS_URL = "https://dogecoinarcade.com/bugs"

#: The dashes-and-colons row under a table's header. It has to contain a
#: dash: a table with no header starts `| | |`, which is pipes and spaces
#: and would otherwise be eaten as a rule -- and then the first real row
#: becomes a heading and reads as one.
_RULE = re.compile(r"^\|[\s:|-]*-[\s:|-]*\|?$")

_INLINE = (
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
)


def inline(text: str) -> str:
    """Escape first, then mark up: a document is text, not markup."""
    out = html.escape(text, quote=False)
    for pattern, replacement in _INLINE:
        out = pattern.sub(replacement, out)
    return out


def sections() -> list[dict]:
    """The document as (title, html) pairs, one per `## ` heading.

    The lead paragraphs before the first heading come back as a section with
    no title, so nothing in the file is silently dropped.
    """
    text = GUIDE.read_text(encoding="utf-8") if GUIDE.exists() else ""
    found: list[dict] = [{"title": "", "body": []}]
    for line in text.splitlines():
        if line.startswith("## "):
            found.append({"title": line[3:].strip(), "body": []})
        elif line.startswith("# "):
            continue                      # the document's own title
        else:
            found[-1]["body"].append(line)
    return [{"title": s["title"], "html": render(s["body"])}
            for s in found if any(line.strip() for line in s["body"])]


def render(lines: list[str]) -> str:
    """One section's lines as HTML. Blocks are separated by blank lines."""
    out: list[str] = []
    bullets: list[str] = []
    paragraph: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{inline(b)}</li>" for b in bullets)
                       + "</ul>")
            bullets.clear()
        if table:
            # A pipe table. The separator row is the one made of dashes and
            # colons; the row before it, if there is one, is the header.
            rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
                    for line in table if not _RULE.match(line)]
            header = bool(rows) and any(cell for cell in rows[0])
            rows = [r for r in rows if any(cell for cell in r)]
            body = []
            for n, cells in enumerate(rows):
                tag = "th" if header and n == 0 else "td"
                body.append("<tr>" + "".join(
                    f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            out.append("<table>" + "".join(body) + "</table>")
            table.clear()
        if paragraph:
            out.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped == "---":
            flush()
        elif stripped.startswith("### "):
            flush()
            out.append(f"<h3>{inline(stripped[4:])}</h3>")
        elif stripped.startswith("|"):
            if paragraph or bullets:
                flush()
            table.append(stripped)
        elif stripped.startswith(("* ", "- ")):
            if paragraph:
                flush()
            bullets.append(stripped[2:])
        elif bullets and line.startswith(("  ", "\t")):
            bullets[-1] += " " + stripped     # a bullet wrapped onto more lines
        else:
            paragraph.append(stripped)
    flush()
    return "".join(out)
