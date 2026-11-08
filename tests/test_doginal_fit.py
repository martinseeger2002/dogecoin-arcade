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

    def content(self, key, follow=True):
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


class _Slow:
    def __init__(self, exc):
        self.exc = exc

    def content(self, key, follow=True):
        raise self.exc


def test_a_piece_its_chain_is_still_reading_says_so_and_is_asked_for_again(client, monkeypatch):
    """2026-11-08: half a Bells collection's tiles showed {"error":"no such inscription"} on a Lite node that borrows
    Bells -- long pieces read over the mesh gave up after 25 s, and the frame showed the 404's JSON."""
    client = client[0]
    from arcade import funding
    monkeypatch.setattr(funding, "GUARD", _Slow(doginals.Unknown("the chain is busy reading other inscriptions; try again")))
    raw = client.get(f"/content/{ID}")
    assert raw.status_code == 503 and raw.headers["retry-after"] and raw.headers["cache-control"] == "no-store"
    note = client.get(f"/content/{ID}/fit")
    assert note.status_code == 503 and note.headers["content-type"].startswith("text/html")
    assert "Reading from the chain" in note.text and '"?tries=1"' in note.text and "error" not in note.text
    assert note.headers["cache-control"] == "no-store" and "sandbox" in note.headers["content-security-policy"]
    last = client.get(f"/content/{ID}/fit?tries=6")
    assert "Not read yet" in last.text and "<script" not in last.text, "a few tries, then it stops asking"
    for now in (TimeoutError("timed out"), doginals.Unknown("not on this chain: Bellscoin: the arcades sharing it did "
                "not answer just now"), doginals.Unknown("not on this chain: no arcade sharing bel answered "
                "(5631a42e29e0: the sharing arcade said: busy)")):
        monkeypatch.setattr(funding, "GUARD", _Slow(now))
        assert client.get(f"/content/{ID}").status_code == 503, now


def test_a_piece_no_chain_has_gets_a_note_not_json(client, monkeypatch):
    client = client[0]
    from arcade import funding
    monkeypatch.setattr(funding, "GUARD", _Slow(ValueError("no ord envelope in that transaction")))
    assert client.get(f"/content/{ID}").status_code == 404
    note = client.get(f"/content/{ID}/fit", follow_redirects=False)
    assert note.status_code == 404 and "Not on this arcade" in note.text and "<script" not in note.text
    monkeypatch.setattr(funding, "GUARD", _Slow(doginals.Unknown("not on this chain: no such transaction")))
    assert client.get(f"/content/{ID}").status_code == 404
