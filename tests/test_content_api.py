"""The API an inscribed page can call.

Inscribed code is written by strangers and runs in the browser of a wallet that
can spend. These endpoints are the only same-origin URLs in the application that
answer a cross-origin request, and every one of them is a read.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

CHAIN_ENDPOINTS = ["/r/blockheight", "/r/blocktime", "/r/inscriptions",
                   "/r/tag/somebody", "/r/address/nSomebody",
                   "/r/balances/nSomebody"]


@pytest.mark.parametrize("path", CHAIN_ENDPOINTS)
def test_a_sandboxed_page_can_reach_the_chain_endpoints(client, path):
    """A frame with no allow-same-origin has an opaque origin, so without CORS
    an inscribed page could not read even its own metadata."""
    app, _ = client
    response = app.get(path)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_the_rest_of_the_wallet_says_nothing_about_cors(client):
    """Which is what stops an inscription reading it: a cross-origin fetch
    without permission cannot see the answer."""
    app, _ = client
    for path in ("/", "/tokens", "/wallet", "/messages", "/remote"):
        assert "access-control-allow-origin" not in app.get(path).headers, path


def test_content_is_served_sandboxed_and_never_sniffed(client, monkeypatch):
    """Bytes a stranger wrote. `sandbox` in the policy applies to the response
    whatever frame it lands in, so it holds even opened directly in a tab."""
    app, state = client

    class FakeIndex:
        def inscription_content(self, key):
            return "text/html", b"<script>alert(1)</script>"
        def inscription(self, key):
            return None

    from arcade.web import app as webapp
    monkeypatch.setattr(webapp, "_CONTENT_INDEX_FOR_TESTS", FakeIndex(), raising=False)

    from arcade.web import content as contentlib
    response = contentlib.content(FakeIndex(), "0")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.media_type == "text/html"


def test_a_type_we_will_not_render_is_handed_over_as_a_download(client):
    """An inscription is arbitrary bytes, and guessing at them is how a text
    file becomes a script."""
    from arcade.web import content as contentlib

    class Odd:
        def inscription_content(self, key):
            return "application/x-msdownload", b"MZ..."
        def inscription(self, key):
            return None

    response = contentlib.content(Odd(), "0")
    assert response.headers["content-disposition"] == "attachment"
    assert response.media_type == "application/octet-stream"


def test_content_this_node_did_not_keep_says_so_and_how_to_check_it(client):
    """Not held here is not the same as does not exist. The hash and the
    length are always kept, so it can be fetched back and proved."""
    from arcade.web import content as contentlib

    class Described:
        def inscription_content(self, key):
            return None
        def inscription(self, key):
            return {"sha256": "ab" * 32, "content_len": 1234}

    response = contentlib.content(Described(), "7")
    assert response.status_code == 404
    body = response.body.decode()
    assert "did not keep" in body and "ab" * 32 in body and "1234" in body


def test_an_inscription_is_named_by_number_or_by_txid():
    from arcade.web import content as contentlib

    assert contentlib._key("42") == 42
    assert contentlib._key("AB" * 32) == ("ab" * 32)


def test_the_wallet_endpoint_can_be_turned_off(client):
    """A balance is public either way. WHICH address is yours is the one thing
    the chain does not say, so that is the part with a switch."""
    app, state = client
    assert app.get("/r/wallet").status_code == 200

    state.inscription_wallet_access = False
    refused = app.get("/r/wallet")
    assert refused.status_code == 403
    assert "does not tell inscriptions who is looking" in refused.text
    assert refused.headers["access-control-allow-origin"] == "*", (
        "a page has to be able to read the refusal too")


def test_every_content_endpoint_is_a_read(client):
    """None of them may change anything: that is the whole boundary."""
    app, _ = client
    for path in CHAIN_ENDPOINTS + ["/r/wallet", "/content/0"]:
        assert app.post(path).status_code in (404, 405), path


def test_a_missing_inscription_is_a_404_not_an_error_page(client):
    app, _ = client
    for path in ("/content/999999", "/r/inscription/999999", "/r/metadata/999999"):
        response = app.get(path)
        assert response.status_code == 404
        assert response.json()["error"] == "no such inscription"
