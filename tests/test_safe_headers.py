"""What every answer tells the browser (2026-11-08, security review): nosniff, a Referrer-Policy, HSTS over https,
no-store on account answers, and a page CSP with no plugins and no foreign <base>."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_claim import _sign_in                          # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}


def test_every_answer_says_nosniff_and_keeps_its_addresses_to_itself(client):
    app, _ = client
    for path in ("/join", "/coins.js", "/auth/challenge"):
        got = app.get(path, headers=LOCAL)
        assert got.headers.get("x-content-type-options") == "nosniff", path
        assert got.headers.get("referrer-policy") == "same-origin", path
    page = app.get("/join", headers=LOCAL).headers.get("content-security-policy")
    assert "object-src 'none'" in page and "base-uri 'self'" in page and "frame-ancestors 'self'" in page


def test_hsts_only_over_https(client):
    app, _ = client
    assert "strict-transport-security" not in app.get("/join", headers=LOCAL).headers
    over = app.get("/join", headers={"host": "wallet.example.com", "x-forwarded-proto": "https"})
    assert over.headers.get("strict-transport-security", "").startswith("max-age=")


def test_an_accounts_answers_are_never_cached(client):
    app, _ = client
    _sign_in(app)
    for path in ("/account/coins/list", "/account/standing/fillable", "/auth/who"):
        got = app.get(path)
        assert "no-store" in got.headers.get("cache-control", ""), (path, got.headers.get("cache-control"))
    assert "private" not in (app.get("/coins.js").headers.get("cache-control") or ""), "shared files keep theirs"
