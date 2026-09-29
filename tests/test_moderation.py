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
    assert "Checking this post for sensitive material" in body
    covered = body[body.index('<details class="covered checking"'):]
    covered = covered[:covered.index("</details>")]
    assert "not judged yet" in covered, "its words are behind the cover, shown on a tap (2026-09-28)"
    assert body.count("not judged yet") == 1, "and nowhere else on the page"


def test_a_better_prompt_asks_again_about_what_the_old_one_passed(tmp_path, model):
    """Verdicts are kept per content, so without a prompt version a better question
    never reached anything already judged: "Fuck my asshole" stayed "ok -- profanity
    only" after the prompt learned better (2026-09-25)."""
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    digest = mod.digest_of("Fuck my asshole")
    screen.conn.execute("INSERT INTO verdict (digest, kind, verdict, reason, model, checked_at,"
                        " prompt) VALUES (?,?,?,?,?,?,?)",
                        (digest, "text", "ok", "profanity only", "m", 0, "an-older-prompt"))
    screen.conn.commit()
    assert screen.known(digest) is None, "asked again, not trusted"
    model.says = '{"verdict": "sensitive", "reason": "crude sexual reference"}'
    assert screen.check_text("Fuck my asshole", now=True) == mod.SENSITIVE
    assert screen.known(digest) == mod.SENSITIVE


def test_an_illegal_verdict_is_never_reopened_by_a_new_prompt(tmp_path):
    screen = mod.Screen(tmp_path, None)
    screen.conn.execute("INSERT INTO verdict (digest, kind, verdict, reason, model, checked_at,"
                        " prompt) VALUES (?,?,?,?,?,?,?)",
                        ("cd" * 32, "image", "illegal", "x", "m", 0, "an-older-prompt"))
    screen.conn.commit()
    assert screen.known("cd" * 32) == mod.ILLEGAL


def test_a_table_from_before_the_prompt_column_still_opens(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "moderation.sqlite")
    old.executescript("CREATE TABLE verdict (digest TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                      " verdict TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
                      " model TEXT NOT NULL DEFAULT '', checked_at INTEGER NOT NULL);"
                      "INSERT INTO verdict VALUES ('ee', 'text', 'ok', '', 'm', 0);")
    old.commit(); old.close()
    screen = mod.Screen(tmp_path, None)
    assert screen.known("ee") is None, "judged under no known prompt: asked again"


# --- when the screening is wrong (2026-09-28) ---------------------------------


def test_the_operator_can_clear_a_verdict_and_it_stays_cleared(tmp_path, model):
    """"whoever comments on this post gets an ooh can do" came back "sexual
    innuendo"; OOH CAN DO is a token. Cleared once, it stays cleared."""
    model.says = '{"verdict": "sensitive", "reason": "sexual innuendo"}'
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    words = "whoever comments on this post gets an ooh can do"
    assert screen.check_text(words, now=True) == mod.SENSITIVE
    screen.overrule("  " + words + "\n")                  # the same words, however spaced
    assert screen.check_text(words) == mod.OK
    monkey = mod.PROMPT_VERSION
    try:
        mod.PROMPT_VERSION = "a-newer-prompt"
        assert screen.known(mod.digest_of(words)) == mod.OK, "not asked again by a new prompt"
    finally:
        mod.PROMPT_VERSION = monkey
    assert len(model.asked) == 1


def test_an_illegal_verdict_cannot_be_cleared_from_the_feed(tmp_path):
    screen = mod.Screen(tmp_path, None)
    screen.conn.execute("INSERT INTO verdict (digest, kind, verdict, reason, model, checked_at,"
                        " prompt) VALUES (?,?,?,?,?,?,?)",
                        (mod.digest_of("x y z"), "text", "illegal", "x", "m", 0, mod.PROMPT_VERSION))
    screen.conn.commit()
    with pytest.raises(ValueError):
        screen.overrule("x y z")
    assert screen.known(mod.digest_of("x y z")) == mod.ILLEGAL


def _a_ledger(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE property (name TEXT); CREATE TABLE tag (tag TEXT);"
                       "CREATE TABLE collection_item (collection TEXT, name TEXT);")
    conn.execute("INSERT INTO property VALUES ('OOH CAN DO')")
    conn.execute("INSERT INTO property VALUES ('ok')")          # too short to mean anything
    conn.execute("INSERT INTO tag VALUES ('silas')")
    conn.execute("INSERT INTO collection_item VALUES ('Pixel Pals', 'Pal #7')")
    conn.commit()
    conn.close()


def test_the_model_is_told_which_words_are_names_here(tmp_path, model):
    _a_ledger(tmp_path / "test-ledger.sqlite")
    screen = mod.Screen(tmp_path, {"url": model.url, "model": "m"})
    screen.ledgers = [tmp_path / "test-ledger.sqlite", tmp_path / "not-there.sqlite"]
    screen.check_text("whoever comments gets an ooh can do, ask @silas about Pixel Pals", now=True)
    asked = model.asked[-1]["messages"][1]["content"]
    assert '"OOH CAN DO" is a token' in asked
    assert '"@silas" is a person' in asked
    assert '"Pixel Pals" is an NFT collection' in asked
    assert '"ok"' not in asked
    screen.check_text("nothing named in this one", now=True)
    assert "belong to this site" not in model.asked[-1]["messages"][1]["content"]


def test_the_operator_clears_a_covered_post_from_the_feed(client, model):
    app, state = client
    model.says = '{"verdict": "sensitive", "reason": "sexual innuendo"}'
    screen = _screened(state, model)
    a_post(state, "ab" * 32, text="gets an ooh can do")
    screen.check_text("gets an ooh can do", now=True)
    body = app.get("/feed").text
    assert "Sensitive post" in body and ">Not sensitive</button>" in body
    assert app.post("/admin/api/unflag", json={"txid": "ab" * 32}).status_code == 403, \
        "an admin write, from the admin script only"
    said = app.post("/admin/api/unflag", json={"txid": "ab" * 32},
                    headers={"x-arcade-admin": "1"})
    assert said.status_code == 200, said.text
    body = app.get("/feed").text
    assert "Sensitive post" not in body and "gets an ooh can do" in body
    assert app.post("/admin/api/unflag", json={"txid": "cd" * 32},
                    headers={"x-arcade-admin": "1"}).status_code == 404


def test_only_the_operator_is_offered_not_sensitive(client, model):
    app, state = client
    model.says = '{"verdict": "sensitive", "reason": "x"}'
    screen = _screened(state, model)
    a_post(state, "ef" * 32, text="a spicy post")
    screen.check_text("a spicy post", now=True)
    state.public = True
    try:
        body = app.get("/feed").text
        assert "Sensitive post" in body and ">Not sensitive</button>" not in body
    finally:
        state.public = False
