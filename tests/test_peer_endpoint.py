"""A node named by a game's inscribed terms is posted to only at a plain https address on a public host (SadFrog's side,
2026-10-25): `node + path` was used as given, so a game could name this node's own loopback or LAN."""
import pytest
import requests

from arcade.web import app as appmod


@pytest.mark.parametrize("base", ["https://arcade.example.com", "https://arcade.example.com/", "https://arcade.example.com/sub"])
def test_a_plain_https_node_is_addressed(base):
    url = appmod._peer_endpoint(base, "/r/escrow/release")
    assert url.startswith("https://arcade.example.com") and url.endswith("/r/escrow/release") and "?" not in url


@pytest.mark.parametrize("base", ["http://127.0.0.1:8420", "http://127.0.0.1:8420/auth/set-password?",
                                  "https://arcade.example.com/x?y=", "https://arcade.example.com/#f",
                                  "https://u:p@arcade.example.com", "ftp://arcade.example.com", "", "arcade.example.com"])
def test_anything_else_is_refused(base):
    with pytest.raises(ValueError):
        appmod._peer_endpoint(base, "/r/escrow/release")


def test_an_https_name_that_points_inward_is_not_posted_to(monkeypatch):
    monkeypatch.setattr(appmod.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("127.0.0.1", p))])
    with pytest.raises(requests.ConnectionError):
        appmod._post_arcade(appmod._peer_endpoint("https://inward.example.com", "/r/escrow/release"), json={})
