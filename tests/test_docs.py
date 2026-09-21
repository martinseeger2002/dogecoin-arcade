"""The documentation, served by every instance.

A copy of the program should be a copy of its manual too: somebody offline,
or on a clone, or who does not know there is a website still has to be able
to read what this does (D-152).
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import guide                                     # noqa: E402

#: Where the documents are written. The copy under the package is what
#: ships; this is the one a person edits.
SOURCE = pathlib.Path("/home/you/docs")


def test_every_document_is_offered(client):
    app, _ = client
    body = app.get("/docs").text
    for page in guide.pages():
        assert f'/docs/{page["name"]}' in body, page["name"]
    assert "Every decision, and why" in body, "the long one is offered too"


def test_each_one_renders(client):
    app, _ = client
    for page in guide.pages():
        answer = app.get(f'/docs/{page["name"]}')
        assert answer.status_code == 200, page["name"]
        for marker in ("Traceback", "NameError", "KeyError"):
            assert marker not in answer.text, f'{page["name"]} leaked {marker}'


def test_the_longest_one_arrives_whole(client):
    """DECISIONS.md is a quarter of a megabyte and a hundred and fifty
    sections. A renderer that silently drops the tail would look fine on
    every other page here."""
    app, _ = client
    text = guide.page("DECISIONS.md")
    sections = guide.document(text)
    assert len(sections) > 100
    body = app.get("/docs/DECISIONS.md").text
    assert "D-001" in body or "D-002" in body, "the first decision is there"
    assert sections[-1]["title"] in body, "and so is the last"


def test_a_path_nobody_shipped_is_refused():
    """The name comes out of a URL, so it is never used as a path."""
    for name in ("../../../etc/passwd", "../pyproject.toml", "/etc/passwd",
                 "messaging/../../setup.py", "", "..", "nope.md"):
        assert guide.page(name) is None, name


def test_only_markdown_is_served(tmp_path):
    (guide.DOCS / "not-a-doc.txt").write_text("secrets")
    try:
        assert guide.page("not-a-doc.txt") is None
    finally:
        (guide.DOCS / "not-a-doc.txt").unlink()


def test_the_guide_link_still_works(client):
    """Every release note and half the templates point at /guide."""
    app, _ = client
    answer = app.get("/guide", follow_redirects=False)
    assert answer.status_code in (302, 303, 307)
    assert "features.md" in answer.headers["location"]


def test_the_nav_offers_docs(client):
    app, _ = client
    body = app.get("/").text
    assert 'href="/docs"' in body and ">Docs<" in body


def test_what_ships_is_what_was_written():
    """The copy under the package is made at release time, so it is checked
    the way `guide.md` has always been: byte for byte, or the application
    ships a version of the documentation nobody wrote."""
    if not SOURCE.exists():
        pytest.skip("the source documents are not on this machine")
    drifted = []
    for shipped in sorted(guide.DOCS.rglob("*.md")):
        name = shipped.relative_to(guide.DOCS)
        written = SOURCE / name
        if not written.exists():
            drifted.append(f"{name}: shipped but not in docs/")
        elif written.read_bytes() != shipped.read_bytes():
            drifted.append(f"{name}: differs from docs/{name}")
    assert drifted == [], drifted


def test_nothing_written_is_left_behind():
    """The other direction: a document added to `docs/` and not copied in is
    a document nobody reading a clone will ever see."""
    if not SOURCE.exists():
        pytest.skip("the source documents are not on this machine")
    #: features.md ships as `templates/guide.md` instead, because the guide
    #: page has always read it from there. remote-access.md described the
    #: Cloudflare tunnel, which no longer exists (D-151).
    ELSEWHERE = {"features.md", "remote-access.md"}
    missing = []
    for written in sorted(SOURCE.rglob("*.md")):
        name = written.relative_to(SOURCE)
        if str(name) in ELSEWHERE:
            continue
        if not (guide.DOCS / name).exists():
            missing.append(str(name))
    assert missing == [], f"written but not shipped: {missing}"
