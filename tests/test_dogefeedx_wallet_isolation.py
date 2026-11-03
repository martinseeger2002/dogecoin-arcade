"""Switching the Profile wallet drops the previous wallet from every module that reads it.

Messages, Bookmarks, Settings and Home are not among them since 2026-11-03: they are the arcade's own account (its
sealed letters, its profile on the chain, its posts), not a watch-only address (dogefeedx/messages.html,
settings.html, feed.html)."""
import json
import pathlib
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")
from playwright.sync_api import sync_playwright                     # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_realtime_web import make_state                           # noqa: E402

from arcade.web.app import create_app                              # noqa: E402

A = "DAlphaWalletIsolation11111111111111"
B = "DBetaWalletIsolation222222222222222"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    def __init__(self, app, port):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", lifespan="off",
            timeout_graceful_shutdown=1.0))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        end = time.time() + 10
        while not self.server.started and time.time() < end:
            time.sleep(0.05)
        assert self.server.started

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def base(tmp_path, no_nodes):
    port = _free_port()
    server = _Server(create_app(make_state(tmp_path, "isolate")), port)
    yield f"http://127.0.0.1:{port}"
    server.stop()


def _chrome(playwright):
    try:
        return playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception:
        return playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])


def _holdings(addr):
    alpha = addr == A
    name = "AlphaDogs" if alpha else "BetaDogs"
    return {
        "imported": addr in (A, B),
        "addresses": [addr] if addr in (A, B) else [],
        "balance_doge": "11" if alpha else "22",
        "collections": [{
            "name": name,
            "slug": name.lower(),
            "count": 1,
            "pieces": [{"title": name + " #1", "edition": "1", "image": ""}],
        }] if addr in (A, B) else [],
        "tokens": [],
        "names": [{"name": name.lower() + ".doge"}] if addr in (A, B) else [],
        "empty": "" if addr in (A, B) else "Import a watch-only address on this node.",
    }


def _seed():
    messages_a = [
        {"id": str(i), "to": "@alpha", "text": "ALPHA-SECRET-THREAD", "unread": True}
        for i in range(5)
    ]
    notes_a = [{"bucket": "likes", "text": "ALPHA-NOTE", "unread": True} for _ in range(3)]
    return f"""
      localStorage.setItem('arcade.deskview', 'dogefeedx');
      if (window.top === window) localStorage.setItem('dogefeedx.wallet', {json.dumps(A)});
      localStorage.setItem('dogefeedx.messages::' + {json.dumps(A)}, {json.dumps(json.dumps(messages_a))});
      localStorage.setItem('dogefeedx.messages::' + {json.dumps(B)}, '[]');
      localStorage.setItem('dogefeedx.notifications::' + {json.dumps(A)}, {json.dumps(json.dumps(notes_a))});
      localStorage.setItem('dogefeedx.notifications::' + {json.dumps(B)}, '[]');
      localStorage.setItem('dogefeedx.bookmarks::' + {json.dumps(A)}, {json.dumps(json.dumps([{"id": "a", "handle": "alpha", "text": "ALPHA-MARK"}]))});
      localStorage.setItem('dogefeedx.bookmarks::' + {json.dumps(B)}, '[]');
      localStorage.setItem('dogefeedx.bookmarks', {json.dumps(json.dumps([{"id": "leak", "handle": "leak", "text": "LEAKED-GLOBAL-BOOKMARK"}]))});
      localStorage.setItem('dogefeedx.settings::' + {json.dumps(A)}, {json.dumps(json.dumps({"displayName": "Alpha Name", "dns": "alpha.doge", "website": "https://alpha.example", "n-likes": True, "n-comments": True, "n-reposts": True, "n-market": True}))});
      localStorage.setItem('dogefeedx.settings::' + {json.dumps(B)}, {json.dumps(json.dumps({"displayName": "Beta Name", "dns": "beta.doge", "website": "https://beta.example", "n-likes": False, "n-comments": True, "n-reposts": True, "n-market": True}))});
      localStorage.setItem('dogefeedx.profile::' + {json.dumps(A)}, {json.dumps(json.dumps({"displayName": "Alpha Name", "dns": "alpha.doge", "website": "https://alpha.example"}))});
      localStorage.setItem('dogefeedx.profile::' + {json.dumps(B)}, {json.dumps(json.dumps({"displayName": "Beta Name", "dns": "beta.doge", "website": "https://beta.example"}))});
      localStorage.setItem('dogefeedx.inscribe::' + {json.dumps(A)}, {json.dumps(json.dumps({"note": "ALPHA-INSCRIBE"}))});
      localStorage.setItem('dogefeedx.inscribe::' + {json.dumps(B)}, {json.dumps(json.dumps({"note": "BETA-INSCRIBE"}))});
      localStorage.setItem('dogefeedx.localBarks::' + {json.dumps(A)}, {json.dumps(json.dumps([{"id": "1", "address": A, "text": "ALPHA-BARK", "images": []}]))});
      localStorage.setItem('dogefeedx.localBarks::' + {json.dumps(B)}, {json.dumps(json.dumps([{"id": "2", "address": B, "text": "BETA-BARK", "images": []}]))});
      localStorage.setItem('dogefeedx.badge.messages::' + {json.dumps(A)}, '5');
      localStorage.setItem('dogefeedx.badge.notifications::' + {json.dumps(A)}, '3');
      localStorage.setItem('dogefeedx.badge.messages::' + {json.dumps(B)}, '0');
      localStorage.setItem('dogefeedx.badge.notifications::' + {json.dumps(B)}, '0');
    """


def _win(page, path):
    # A window that was just closed stays in the page, hidden, for a short while.
    # A dock click on the front app minimizes it, so only click when none is showing.
    return page.locator(f'.dk-win:not([hidden]):has(iframe[src="{path}"])').first


def _body(page, path):
    body = _win(page, path).frame_locator("iframe").locator("body")
    body.wait_for(timeout=20000)
    return body


def _open(page, title, path):
    if _win(page, path).count() == 0:
        page.locator(f'#dk-dock .dfx-dock-app[title="{title}"]').click()
    return _body(page, path)


def test_switching_wallets_drops_the_previous_wallet(base):
    with sync_playwright() as playwright:
        browser = _chrome(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.add_init_script(_seed())
        page.route("**/dogefeedx/api/wallet/addresses", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"addresses": [A, B], "source": "node"})))
        page.route("**/account/badges", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "/messages": 99, "/me/messages": 99,
                "/notifications": 99, "/me/notifications": 99,
                "/exchange": 99,
            })))
        page.route("**/dogefeedx/api/local/notifications*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"signed_in": True, "items": [
                {"bucket": "likes", "text": "SERVER-LEAK", "unread": True},
            ]})))

        def holdings(route):
            addr = (parse_qs(urlparse(route.request.url).query).get("address") or [""])[0]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_holdings(addr)))

        page.route("**/dogefeedx/api/wallet/holdings*", holdings)
        page.goto(base + "/desktop", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.documentElement.getAttribute('data-skin') === 'dogefeedx'"
            " && document.querySelectorAll('#dk-dock .dfx-dock-app').length === 16")

        def badge(title):
            return page.locator(f'#dk-dock .dfx-dock-app[title="{title}"] .dfx-badge')

        badge("Messages").wait_for(timeout=10000)
        assert badge("Messages").inner_text() == "5"
        assert badge("Notifications").inner_text() == "3"
        assert page.locator("#dk-dock .dfx-badge", has_text="99").count() == 0

        modules = [
            ("Notifications", "/dogefeedx/notifications", "ALPHA-NOTE"),
            ("Inscribe", "/dogefeedx/inscribe", "ALPHA-INSCRIBE"),
            ("NFTs", "/dogefeedx/nfts", "AlphaDogs"),
            ("Files", "/dogefeedx/files", "AlphaDogs"),
            ("Wallet", "/dogefeedx/wallet", "11 DOGE"),
        ]
        for title, path, needle in modules:
            body = _open(page, title, path)
            body.get_by_text(needle, exact=False).first.wait_for(timeout=15000)

        leaks = (
            "BETA-SECRET-THREAD", "BETA-NOTE", "BETA-MARK", "BETA-INSCRIBE",
            "BETA-BARK", "BetaDogs", "LEAKED-GLOBAL-BOOKMARK", "SERVER-LEAK",
        )
        for title, path, _needle in modules:
            text = _open(page, title, path).inner_text()
            for leak in leaks:
                assert leak not in text, (path, leak)

        profile = _open(page, "Profile", "/dogefeedx/profile")
        assert profile.locator("#df-wallet-select").input_value() == A
        assert _open(page, "Inscribe", "/dogefeedx/inscribe").locator("#inscribe-note").input_value() == "ALPHA-INSCRIBE"

        profile = _open(page, "Profile", "/dogefeedx/profile")
        assert profile.locator("#df-wallet-select").input_value() == A
        profile.locator("#df-wallet-select").select_option(B)

        gone = {
            "/dogefeedx/notifications": "ALPHA-NOTE",
            "/dogefeedx/inscribe": "ALPHA-INSCRIBE",
            "/dogefeedx/nfts": "AlphaDogs",
            "/dogefeedx/files": "AlphaDogs",
            "/dogefeedx/wallet": "11 DOGE",
        }
        arrived = {
            "/dogefeedx/notifications": "No notifications for this wallet.",
            "/dogefeedx/inscribe": "BETA-INSCRIBE",
            "/dogefeedx/nfts": "BetaDogs",
            "/dogefeedx/files": "BetaDogs",
            "/dogefeedx/wallet": "22 DOGE",
        }
        title_of = {path: title for title, path, _needle in modules}
        for path, needle in arrived.items():
            _open(page, title_of[path], path).get_by_text(needle, exact=False).first.wait_for(timeout=15000)
        for path, needle in gone.items():
            text = _open(page, title_of[path], path).inner_text()
            assert needle not in text, (path, needle, text[:500])
        for leak in ("LEAKED-GLOBAL-BOOKMARK", "SERVER-LEAK", "ALPHA-SECRET-THREAD"):
            for path, title in title_of.items():
                assert leak not in _open(page, title, path).inner_text(), (path, leak)

        profile = _open(page, "Profile", "/dogefeedx/profile")
        assert profile.locator("#df-wallet-select").input_value() == B
        assert _open(page, "Notifications", "/dogefeedx/notifications").locator("#note-list").get_attribute("data-wallet") == B
        assert _open(page, "Files", "/dogefeedx/files").locator("#files-body").get_attribute("data-wallet") == B
        assert _open(page, "NFTs", "/dogefeedx/nfts").locator("#df-nfts").get_attribute("data-wallet") == B
        assert _open(page, "Wallet", "/dogefeedx/wallet").locator("#wallet-grid").get_attribute("data-wallet") == B
        assert _open(page, "Inscribe", "/dogefeedx/inscribe").locator("#inscribe-for").get_attribute("data-wallet") == B
        page.wait_for_function("() => document.querySelectorAll('#dk-dock .dfx-badge').length === 0")
        assert badge("Messages").count() == 0
        assert badge("Notifications").count() == 0
        browser.close()
