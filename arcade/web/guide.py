"""The written guide, inside the application.

A user who is offline, or behind the remote tunnel, or who does not know
there is a website, still has to be able to find out what the thing in front
of them does. So the guide ships with the application rather than only on the
site: `templates/guide.md` is the same file the site publishes as Features,
copied in at release time and checked by a test (D-047).

There used to be a second, shorter guide written by hand in this module, kept
in step with the document by a test that compared their section titles. Two
descriptions of one program is one too many: the short one was always a
little behind, and the test only noticed when a heading changed, never when a
sentence went stale.

Markdown is rendered here rather than by a library. The subset the document
uses is small -- headings, bullets, emphasis, code, links, rules -- and a
wallet should not grow a dependency to show its own help.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

GUIDE = Path(__file__).parent / "templates" / "guide.md"

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
