"""An arcade whose pages name has no DNS record frames its pages from itself (2026-10-19: every game on
pepecoinarcade.com was a white screen; pages.pepecoinarcade.com was set and never routed)."""
from test_web import app_state  # noqa: F401  (fixture re-export)


def test_a_pages_name_with_no_dns_frames_from_here(app_state, monkeypatch):
    from arcade.web.state import AppState
    monkeypatch.undo()                                      # the real lookup, not conftest's
    app_state.set_setting("pages_host", "pages.no-such-name.invalid")
    assert app_state.pages_origin == "", "nothing resolves .invalid: framed from here"
    app_state.set_setting("pages_host", "localhost")
    assert app_state.pages_origin == "https://localhost"
