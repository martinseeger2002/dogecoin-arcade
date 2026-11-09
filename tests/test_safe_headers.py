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


def _html_pages(app):
    """Every GET route with no path parameter, as the operator at the machine sees it, that answers a page."""
    framework = {app.app.openapi_url, app.app.docs_url, app.app.redoc_url, app.app.swagger_ui_oauth2_redirect_url}
    for route in app.app.routes:
        path = getattr(route, "path", "")
        if "GET" not in (getattr(route, "methods", None) or ()) or "{" in path or path in framework:
            continue
        try:
            got = app.get(path, headers=LOCAL, follow_redirects=False)
        except Exception:                                  # noqa: BLE001 -- a route that needs a node: not a page here
            continue
        if got.headers.get("content-type", "").startswith("text/html"):
            yield path, got


def test_every_page_says_who_may_frame_it(client):
    """2026-11-09, checking the review's fixes on the live arcades: /lite (the ArcadeCore window and its wallet), /me
    and / are drawn outside render() and sent no policy, so any site could frame them. Every page says one now."""
    app, _ = client
    seen, bare = [], []
    for path, got in _html_pages(app):
        seen.append(path)
        if "frame-ancestors" not in got.headers.get("content-security-policy", ""):
            bare.append(path)
    assert {"/", "/lite", "/join"} <= set(seen), seen
    assert not bare, f"pages any site could frame: {bare}"
    for path in ("/lite", "/"):
        policy = app.get(path, headers=LOCAL, follow_redirects=False).headers["content-security-policy"]
        assert "frame-ancestors 'self'" in policy and "object-src 'none'" in policy, (path, policy)


def test_a_page_with_its_own_policy_keeps_it(client):
    """The restore kit forbids every frame; the header middleware must not add a second, looser policy beside it."""
    app, _ = client
    got = app.get("/recover", headers=LOCAL)
    policies = [v for k, v in got.headers.items() if k.lower() == "content-security-policy"]
    assert len(policies) == 1 and "frame-ancestors 'none'" in policies[0], policies
