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


# --- the inscription reference ------------------------------------------------

API_DOC = pathlib.Path("/home/you/docs/inscription-api.md")


@pytest.mark.skipif(not API_DOC.exists(), reason="the written docs are not here")
def test_every_endpoint_the_reference_promises_exists():
    """Documentation that describes an endpoint nobody built is worse than
    none: somebody writes against it and finds out at run time."""
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    text = API_DOC.read_text()
    promised = set(re.findall(r"`?GET (/[a-z/<>?=&\w.]+)", text))
    promised |= set(re.findall(r"fetch\('(/[^']+)'\)", text))
    # An ellipsis means "a path of this shape", not a path. Those are prose.
    promised = {path for path in promised if "\u2026" not in path}

    nowhere = pathlib.Path("/nonexistent")
    app = create_app(AppState(
        home=pathlib.Path("/tmp"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="T", datadir=nowhere),
        ledger=ChainContext(network="main", role="ledger",
                            label="M", datadir=nowhere)))
    routes = {getattr(route, "path", "") for route in app.routes}

    def known(path: str) -> bool:
        path = path.split("?")[0].rstrip("/")
        for route in routes:
            shape = re.sub(r"\{[^}]+\}", "<>", route)
            if re.sub(r"<[^>]+>", "<>", path) == shape:
                return True
        return False

    missing = sorted(p for p in promised if not known(p))
    assert missing == [], f"the reference promises endpoints that do not exist: {missing}"


@pytest.mark.skipif(not API_DOC.exists(), reason="the written docs are not here")
def test_the_reference_states_the_sandbox_that_is_actually_set():
    """The claims about what an inscribed page cannot do are the load-bearing
    part of that document. They must match the headers the code sends."""
    from arcade.web import content

    text = API_DOC.read_text()
    policy = content.CONTENT_HEADERS["Content-Security-Policy"]
    for directive in ("sandbox allow-scripts", "connect-src 'self'",
                      "object-src 'none'", "form-action 'none'"):
        assert directive in policy, f"{directive} is not in the policy any more"
        assert directive in text, f"{directive} is claimed nowhere in the reference"

    assert "allow-same-origin" not in policy
    assert "allow-same-origin" not in content.CONTENT_HEADERS.get("sandbox", "")
    assert "cannot phone home" in text or "cannot phone home." in text
