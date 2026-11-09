"""A wizard's preview is sandboxed like the page it will inscribe, and only this arcade frames it (2026-11-09, security
review): its words come from the query string, so on the arcade's own origin they could run as the arcade."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

LOCAL = {"host": "127.0.0.1:8420"}


def test_the_launchpad_preview_is_sandboxed_and_framed_only_here(client):
    app, _ = client
    got = app.get("/tokens/launchpad/preview?name=x&icon=%27%3E%3Cimg%20src%3Dx%3E", headers=LOCAL)
    policy = got.headers.get("content-security-policy", "")
    assert "sandbox" in policy and "frame-ancestors 'self'" in policy, policy
    assert got.headers.get("cache-control") == "no-store"
