"""The guide, in the application as well as on the site.

A user who is offline, behind the remote tunnel, or who does not know there is
a website still has to be able to find out what the thing in front of them
does. That means the guide cannot live only on the site -- and it means the two
copies must not drift apart.
"""

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import guide                                    # noqa: E402

FEATURES = pathlib.Path("/home/you/docs/features.md")


def test_the_guide_is_in_the_application(client):
    app, _ = client
    body = app.get("/guide").text
    assert body.count("<h2 id=") == len(guide.SECTIONS)
    for section in guide.SECTIONS:
        assert section["title"] in body


def test_it_is_reachable_from_every_page(client):
    app, _ = client
    assert '/guide"' in app.get("/").text


def test_every_section_says_something(client):
    for section in guide.SECTIONS:
        assert section["title"] and section["blurb"], section
        assert section["points"], f"{section['title']} has no points"
        for point in section["points"]:
            assert len(point) > 20, point


@pytest.mark.skipif(not FEATURES.exists(), reason="the written docs are not here")
def test_the_two_copies_have_not_drifted_apart():
    """One of them must not quietly grow a feature the other has never heard
    of. Compared by section, which is the level at which a feature either
    exists or does not."""
    written = {re.sub(r"[^a-z ]", "", line[3:].strip().lower())
               for line in FEATURES.read_text().splitlines()
               if line.startswith("## ")}
    shown = {re.sub(r"[^a-z ]", "", s["title"].lower()) for s in guide.SECTIONS}
    assert shown == written, (
        f"only in the application: {shown - written}\n"
        f"only in the written docs: {written - shown}")


@pytest.mark.skipif(not FEATURES.exists(), reason="the written docs are not here")
def test_the_written_guide_covers_what_the_navigation_offers():
    """A section in the interface with nothing written about it is the shape of
    documentation going stale."""
    from arcade.web.app import NAV

    text = FEATURES.read_text().lower()
    for _, label, _, built in NAV:
        if not built or label in ("Overview", "Guide", "Keys", "Backup"):
            continue
        stem = label.lower().rstrip("s")
        assert stem in text, f"{label} is in the navigation and not in the guide"
