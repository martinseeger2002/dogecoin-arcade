"""Content screening: a model looks at content before this arcade shows it.

2026-09-25: pictures and feed posts go past a model first. Sensitive
material is covered and the viewer may lift the cover; illegal material is
removed and cannot be uncovered; nothing unjudged is shown as if it were fine.
With no `moderation` setting, nothing changes at all.
"""

import io
import json
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_feed_web import a_post                                 # noqa: E402

from arcade import moderation as mod                             # noqa: E402

KEY = "ab" * 32


def _png(colour=(200, 40, 40)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, "PNG")
    return out.getvalue()


PICTURE = _png()


@pytest.fixture
def model():
    """A stand-in for the model: answers each question with `model.says`, and
    keeps what it was asked."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            server.asked.append(asked)
            said = server.says(asked) if callable(server.says) else server.says
            body = json.dumps({"choices": [{"message": {"content": said}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.asked, server.says = [], '{"verdict": "ok", "reason": "fine"}'
    server.url = f"http://127.0.0.1:{server.server_port}/v1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _screened(state, model):
    state.set_setting("moderation", {"url": model.url, "model": "test-model"})
    return state.screen()


def _holding(state, monkeypatch, content_type, body):
    class Index:
        def inscription_content(self, key):
            return content_type, body

        def inscription(self, key):
            return None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain=None: Index())


# --- the screen itself -------------------------------------------------------------


def test_no_setting_means_no_screening(tmp_path):
    screen = mod.Screen(tmp_path, None)
    assert not screen.enabled
    assert screen.check_image("image/png", PICTURE) is None
    assert screen.check_text("anything") is None


def test_a_picture_is_asked_about_once_and_remembered(tmp_path, model):
    model.says = '{"verdict": "sensitive", "reason": "nudity"}'
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    assert screen.check_image("image/png", PICTURE) == mod.SENSITIVE
    assert screen.check_image("image/png", PICTURE) == mod.SENSITIVE
    assert len(model.asked) == 1, "judged per content, not per request"
    parts = model.asked[0]["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # and it survives a restart
    assert mod.Screen(tmp_path, {"url": model.url, "model": "m"}).known(
        mod.digest_of(PICTURE)) == mod.SENSITIVE


def test_an_answer_that_is_not_a_verdict_is_not_taken_as_one(tmp_path, model):
    model.says = "I think it is probably fine?"
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    assert screen.check_image("image/png", PICTURE) is None
    assert screen.known(mod.digest_of(PICTURE)) is None


def test_a_model_that_is_away_leaves_it_checking(tmp_path):
    screen = mod.Screen(tmp_path, {"url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 2})
    assert screen.check_image("image/png", PICTURE) is None


def test_an_illegal_verdict_is_logged_for_the_operator(tmp_path, model):
    model.says = '{"verdict": "illegal", "reason": "minor"}'
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    assert screen.check_image("image/png", PICTURE) == mod.ILLEGAL
    assert mod.digest_of(PICTURE) in (tmp_path / "moderation-illegal.log").read_text()


def test_words_are_judged_in_the_background(tmp_path, model):
    model.says = '{"verdict": "sensitive", "reason": "gore"}'
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    assert screen.check_text("a graphic description") is None      # checking
    deadline = time.time() + 5
    while screen.check_text("a graphic description") is None and time.time() < deadline:
        time.sleep(0.05)
    assert screen.check_text("a graphic description") == mod.SENSITIVE


# --- what the viewer gets ------------------------------------------------------------


def test_unscreened_content_is_served_exactly_as_before(client, monkeypatch):
    app, state = client
    _holding(state, monkeypatch, "image/png", PICTURE)
    response = app.get(f"/content/{KEY}")
    assert response.status_code == 200 and response.content == PICTURE
    assert app.get(f"/moderation/verdicts?ids={KEY}").json() == {"enabled": False, "verdicts": {}}


def test_a_fine_picture_is_served_as_usual(client, monkeypatch, model):
    app, state = client
    _screened(state, model)
    _holding(state, monkeypatch, "image/png", PICTURE)
    response = app.get(f"/content/{KEY}")
    assert response.status_code == 200 and response.content == PICTURE


def test_a_sensitive_picture_is_covered_until_the_viewer_asks(client, monkeypatch, model):
    app, state = client
    model.says = '{"verdict": "sensitive", "reason": "nudity"}'
    _screened(state, model)
    _holding(state, monkeypatch, "image/png", PICTURE)

    covered = app.get(f"/content/{KEY}")
    assert covered.status_code == 200 and covered.content != PICTURE
    assert covered.headers["content-type"] == "image/png"
    assert covered.headers["cache-control"] == "no-store", "or the cover sticks after a reveal"

    shown = app.get(f"/content/{KEY}?reveal=1")
    assert shown.content == PICTURE
    assert app.get(f"/moderation/verdicts?ids={KEY}").json()["verdicts"] == {KEY: "sensitive"}


def test_an_illegal_picture_cannot_be_uncovered(client, monkeypatch, model):
    app, state = client
    model.says = '{"verdict": "illegal", "reason": "minor"}'
    _screened(state, model)
    _holding(state, monkeypatch, "image/png", PICTURE)
    for url in (f"/content/{KEY}", f"/content/{KEY}?reveal=1", f"/content/{KEY}?download=1"):
        response = app.get(url)
        assert response.status_code == 451, url
        assert PICTURE not in response.content


def test_nothing_unjudged_is_shown(client, monkeypatch):
    app, state = client
    state.set_setting("moderation", {"url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 2})
    _holding(state, monkeypatch, "image/png", PICTURE)
    response = app.get(f"/content/{KEY}?reveal=1")
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert PICTURE not in response.content


def test_a_text_inscription_is_screened_too(client, monkeypatch, model):
    app, state = client
    model.says = '{"verdict": "sensitive", "reason": "explicit"}'
    _screened(state, model)
    _holding(state, monkeypatch, "text/plain", b"explicit words")
    assert b"explicit words" not in app.get(f"/content/{KEY}").content
    assert app.get(f"/content/{KEY}?reveal=1").content == b"explicit words"


# --- the feed --------------------------------------------------------------------------


def test_feed_posts_wear_covers(client, model):
    app, state = client
    model.says = lambda asked: (
        '{"verdict": "illegal", "reason": "x"}' if "forbidden" in asked["messages"][1]["content"]
        else '{"verdict": "sensitive", "reason": "x"}' if "spicy" in asked["messages"][1]["content"]
        else '{"verdict": "ok", "reason": "x"}')
    screen = _screened(state, model)
    for n, text in enumerate(["a plain post", "a spicy post", "a forbidden post"]):
        a_post(state, f"{n:064x}", text=text, height=100 + n)
    for text in ("a plain post", "a spicy post", "a forbidden post"):
        screen.check_text(text, now=True)

    body = app.get("/feed").text
    assert "a plain post" in body
    assert "Sensitive post" in body and "a spicy post" in body     # inside the cover
    assert body.index("Sensitive post") < body.index("a spicy post")
    assert "a forbidden post" not in body and "Removed by this arcade" in body


def test_an_unjudged_post_says_it_is_being_checked(client):
    app, state = client
    state.set_setting("moderation", {"url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 1})
    a_post(state, "11" * 32, text="not judged yet")
    body = app.get("/feed").text
    assert "not judged yet" not in body and "Checking this post" in body
