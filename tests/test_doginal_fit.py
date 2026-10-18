"""A Doginal's card frame shows the whole picture (2026-10-18: "Doginal dog art is not showing the art" -- the
pieces are 1000 px PNGs, and a frame shows an image document at its own size, so a card held only a corner of its
background). /content/<id>/fit draws a picture scaled into the frame; anything else goes on to /content/<id>."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade import doginals  # noqa: E402
from arcade.web import app as appmod  # noqa: E402,F401

ID = "ab" * 32 + "i0"


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (1000, 1000), (250, 228, 160)).save(out, "PNG")
    return out.getvalue()


class _Guard:
    def __init__(self, kind, body):
        self.kind, self.body = kind, body

    def content(self, key):
        if key != ID:
            raise doginals.Unknown("not here")
        return self.kind, self.body


def test_a_picture_is_drawn_scaled_into_the_frame(client, monkeypatch):
    client = client[0]
    from arcade import funding
    monkeypatch.setattr(funding, "GUARD", _Guard("image/png", _png()))
    page = client.get(f"/content/{ID}/fit")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert f'<img src="/content/{ID}"' in page.text and "object-fit:contain" in page.text
    assert "sandbox" in page.headers["content-security-policy"], "the same sandbox as the content itself"
    assert client.get(f"/content/{ID}").content[:4] == b"\x89PNG"


def test_a_page_that_draws_itself_is_sent_on_unchanged(client, monkeypatch):
    client = client[0]
    from arcade import funding
    monkeypatch.setattr(funding, "GUARD", _Guard("text/html", b"<!doctype html><p>dog</p>"))
    sent = client.get(f"/content/{ID}/fit", follow_redirects=False)
    assert sent.status_code == 307 and sent.headers["location"] == f"/content/{ID}"


def test_cards_frame_the_fit_page():
    assert doginals.frame_url(ID) == f"/content/{ID}/fit"
