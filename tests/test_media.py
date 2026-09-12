"""What may be rendered inline, and what must never be.

An attachment is chosen by somebody else and shown in the origin that holds the
wallet. A file served as HTML or SVG here runs script with access to everything
the interface can do, so these are the tests where being wrong is expensive.

The rule under test: the type used for rendering comes from the file's own
leading bytes, never from what the sender claimed, and an unrecognised file is
not rendered at all.
"""

import pytest

from arcade import media


RENDERABLE = [
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 24, "image", "image/png"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 24, "image", "image/jpeg"),
    (b"GIF89a" + b"\x00" * 24, "image", "image/gif"),
    (b"GIF87a" + b"\x00" * 24, "image", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 12, "image", "image/webp"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 12, "audio", "audio/wav"),
    (b"ID3\x04\x00\x00" + b"\x00" * 24, "audio", "audio/mpeg"),
    (b"\xff\xfb\x90\x44" + b"\x00" * 24, "audio", "audio/mpeg"),
    (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 24, "video", "video/mp4"),
    (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 24, "audio", "audio/mp4"),
    (b"\x1a\x45\xdf\xa3" + b"\x42\x82\x84webm" + b"\x00" * 24, "video", "video/webm"),
    (b"OggS\x00\x02" + b"\x00" * 48, "audio", "audio/ogg"),
]


@pytest.mark.parametrize("data,kind,mime", RENDERABLE,
                         ids=[m for _, _, m in RENDERABLE])
def test_safe_formats_are_recognised(data, kind, mime):
    found = media.sniff(data)
    assert found is not None
    assert found.kind == kind
    assert found.mime == mime


DANGEROUS = [
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "SVG"),
    (b"<?xml version='1.0'?><svg onload='alert(1)'/>", "SVG with onload"),
    (b"<!DOCTYPE html><html><script>alert(1)</script></html>", "HTML"),
    (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "PDF"),
    (b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 12, "shell script"),
    (b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 12, "Windows executable"),
    (b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 12, "ELF binary"),
    (b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 12, "zip / office document"),
    (b"{\"json\": true}" + b" " * 12, "JSON"),
    (b"\x00\x00\x00\x20ftypHTML" + b"\x00" * 24, "unknown ftyp brand"),
    (b"\x1a\x45\xdf\xa3" + b"nothing recognisable here", "unknown EBML"),
]


@pytest.mark.parametrize("data,label", DANGEROUS, ids=[l for _, l in DANGEROUS])
def test_dangerous_formats_are_never_rendered(data, label):
    """These arrive intact and downloadable. They are simply never inlined."""
    assert media.sniff(data) is None


def test_a_lie_about_the_type_changes_nothing():
    """The declared content type is not an input to this decision at all."""
    html = b"<html><script>alert(document.cookie)</script></html>"
    assert media.sniff(html) is None      # regardless of any claimed image/png


def test_a_png_header_on_a_script_still_renders_as_png():
    """The converse, stated plainly: bytes decide, and PNG bytes decode as PNG.

    A file that begins with a real PNG signature is handed to an image decoder.
    Appending script to it does not make it executable -- the decoder is not an
    interpreter, and this is exactly why the allow-list is of non-executable
    formats rather than of extensions.
    """
    found = media.sniff(b"\x89PNG\r\n\x1a\n" + b"<script>alert(1)</script>" * 4)
    assert found is not None and found.mime == "image/png"


def test_a_file_too_short_to_identify_is_not_rendered():
    assert media.sniff(b"\x89PNG") is None


def test_an_oversized_file_is_offered_rather_than_inlined():
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (media.MAX_INLINE_BYTES + 1)
    assert media.sniff(big) is not None       # it is a PNG
    assert media.renderable(big) is None      # but not one to inline


def test_the_media_policy_forbids_everything_by_default():
    assert "default-src 'none'" in media.MEDIA_CSP
    assert "sandbox" in media.MEDIA_CSP
    assert "script-src" not in media.MEDIA_CSP.replace("default-src 'none'", "")


# --- the served response ------------------------------------------------------
# The `client` fixture lives in test_web.py; recreate it here rather than move it,
# so neither file depends on the other's import order.


@pytest.fixture
def client(tmp_path):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    return TestClient(create_app(state)), state



def _with_attachment(state, data, name="thing.bin", content_type="image/png"):
    with state.store() as store:
        message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\x11" * 32,
                                       "me", b"see attached")
        store.add_attachment(message_id, name, content_type, data)
    return message_id


def test_an_image_is_served_inline_with_its_real_type(client):
    app, state = client
    message_id = _with_attachment(state, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    response = app.get(f"/messages/media/{message_id}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_html_claiming_to_be_an_image_is_not_served_inline(client):
    """The attack this endpoint exists to refuse."""
    app, state = client
    message_id = _with_attachment(state, b"<html><script>alert(1)</script></html>",
                                  name="cat.png", content_type="image/png")

    response = app.get(f"/messages/media/{message_id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/messages/attachment/{message_id}")


def test_an_svg_is_never_served_inline(client):
    """SVG is the single most common way this goes wrong."""
    app, state = client
    message_id = _with_attachment(state, b"<svg onload='alert(1)'></svg>",
                                  name="x.svg", content_type="image/svg+xml")

    response = app.get(f"/messages/media/{message_id}", follow_redirects=False)
    assert response.status_code == 303


def test_a_missing_attachment_is_a_404_not_a_traceback(client):
    assert client[0].get("/messages/media/999999").status_code == 404
