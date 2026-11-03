"""Switch Theme from the desktop's own menus, and a dock that does not repeat itself.

Billy, on a real browser (build 3f01918): the system menu built the theme list and never
showed it, Ubuntu's Start button never opened Switch Theme, /me kept its theme buttons
hidden, and DogeFeedX drew Explore on the wallpaper and Feed twice in the dock.
"""
import pathlib
import socket
import sys
import threading
import time


def _shot(name: str) -> str:
    """Where a test's screenshot goes: DFX_SHOTS, else this machine's temp folder (it was the PR author's /opt/cursor)."""
    import os
    import tempfile
    folder = os.environ.get("DFX_SHOTS") or os.path.join(tempfile.gettempdir(), "dfx-shots")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)

import httpx
import pytest
from nacl.signing import SigningKey

pytest.importorskip("playwright.sync_api", reason="needs playwright")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_realtime_web import make_state                           # noqa: E402

from arcade import accounts                                        # noqa: E402
from arcade.web.app import create_app                              # noqa: E402


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
    server = _Server(create_app(make_state(tmp_path, "theme")), port)
    yield f"http://127.0.0.1:{port}"
    server.stop()


def _session(base):
    """An account, so Your arcade (and its theme buttons) actually renders."""
    key = SigningKey.generate()
    with httpx.Client(base_url=base, timeout=20) as client:
        challenge = client.get("/auth/challenge").json()
        signature = key.sign(accounts.login_message(
            challenge["origin"], challenge["nonce"])).signature
        response = client.post("/auth/login", json={
            "pubkey": key.verify_key.encode().hex(),
            "nonce": challenge["nonce"],
            "signature": signature.hex(),
            "join": True,
            "origin": challenge["origin"],
        })
        assert response.status_code == 200, response.text
        token = client.cookies.get("arcade_session")
        assert token, "sign-in did not set a session"
        return token


def _chrome(playwright):
    try:
        return playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception:
        return playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])


def _skin(page):
    return page.evaluate("document.documentElement.getAttribute('data-skin')")


def _wait_skin(page, skin):
    page.wait_for_function(
        "(want) => document.documentElement.getAttribute('data-skin') === want"
        " && document.getElementById('dk-start')",
        arg=skin, timeout=20000)
    assert _skin(page) == skin


def _open_system_themes(page):
    page.locator("#dk-sys").click()
    page.locator("#dk-ctx [role=menuitem]").filter(has_text="Switch Theme").click()
    sub = page.locator("#dk-sub")
    sub.wait_for(state="visible")
    text = sub.inner_text()
    for need in ("Windows 98", "Ubuntu", "DogeFeedX", "Website"):
        assert need in text, text
    assert sub.evaluate("el => !el.hidden && el.getBoundingClientRect().width > 40")
    return sub


def _pick_theme(page, label):
    page.locator("#dk-sub [role=menuitem]").filter(has_text=label).click()


def _deduped(page):
    data = page.evaluate("""() => ({
      titles: [...document.querySelectorAll('#dk-dock .ub-dockbtn')].map(b => b.title),
      labs: [...document.querySelectorAll('#dk-icons .dk-lab')].map(e => e.textContent.trim())
    })""")
    titles = [t for t in data["titles"] if t and t != "Show Applications"]
    want = ["Home", "Profile", "Files", "Notifications", "Messages", "Explore", "Market",
            "Inscribe", "Wallet", "Launchpad", "NFTs", "Arcade", "Bookmarks", "Meme Library",
            "Leaderboard", "Settings"]
    assert titles == want, titles
    overlap = set(titles) & set(data["labs"])
    assert not overlap, overlap
    for name in ("Home", "Feed", "Explore", "Market", "Files", "Settings"):
        assert name not in data["labs"], data["labs"]


def test_switch_theme_from_every_look_and_the_dock_does_not_repeat(base):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _chrome(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(base + "/desktop")
        _wait_skin(page, "ubuntu")

        # Ubuntu: the system menu's Switch Theme list is on screen, and picking one reloads that look.
        _open_system_themes(page)
        assert "Ubuntu (default)" in page.locator("#dk-sub").inner_text()
        _pick_theme(page, "DogeFeedX")
        _wait_skin(page, "dogefeedx")
        _deduped(page)

        # DogeFeedX: the same system menu switches away, including back through the other looks.
        _open_system_themes(page)
        _pick_theme(page, "Windows 98")
        _wait_skin(page, "w98")

        # Windows 98: Start -> Switch Theme opens the cascade (hover is not required).
        page.locator("#dk-start").click()
        page.locator("#dk-items [role=menuitem]").filter(has_text="Switch Theme").click()
        page.locator("#dk-sub").wait_for(state="visible")
        assert "Ubuntu (default)" in page.locator("#dk-sub").inner_text()
        _pick_theme(page, "Ubuntu (default)")
        _wait_skin(page, "ubuntu")

        # Ubuntu Start is Show Applications, not the classic cascade. Switch Theme lives there.
        page.locator("#dk-start").click()
        page.locator("#dk-apps").wait_for(state="visible")
        assert page.locator("#dk-apps h3", has_text="Switch Theme").is_visible()
        page.locator("#dk-apps .ub-app").filter(has_text="Windows 98").click()
        _wait_skin(page, "w98")

        page.locator("#dk-start").click()
        page.locator("#dk-items [role=menuitem]").filter(has_text="Switch Theme").click()
        _pick_theme(page, "DogeFeedX")
        _wait_skin(page, "dogefeedx")
        _deduped(page)

        # DogeFeedX Start is Show Applications too, and it can return to the default look.
        page.locator("#dk-start").click()
        page.locator("#dk-apps").wait_for(state="visible")
        page.locator("#dk-apps .ub-app").filter(has_text="Ubuntu (default)").click()
        _wait_skin(page, "ubuntu")
        assert page.evaluate("localStorage.getItem('arcade.deskview')") == "ubuntu"
        assert errors == []
        browser.close()



#: One picture, as this arcade serves an inscription's content (/content/<id>).
_PIC_ID = "d" * 64 + "i0"


def _arcade_rows(page):
    """The Market's collections and the Launchpad's mints, shaped as this arcade's own indexes answer them (the
    DogeFeedX windows read nothing else since 2026-11-03), with their picture from /content/."""
    import base64
    import json
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    page.route(f"**/content/{_PIC_ID}", lambda route: route.fulfill(status=200, content_type="image/png", body=png))
    page.route("**/dogefeedx/api/market/doginals", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"source": "arcade", "stats": {"collections": "1"},
            "collections": [{"id": "much-dogs", "slug": "much-dogs", "name": "Much Dogs", "item_count": 100,
                             "holder_count": 40, "image": f"/content/{_PIC_ID}", "href": "/exchange/ord/much-dogs",
                             "floor": "12 DOGE", "listed": 3, "sales": 0, "verified": True}]})))
    page.route("**/dogefeedx/api/memes*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"source": "arcade", "offset": 0, "more": False,
            "memes": [{"id": f"m{n}", "name": f"#{n}", "image": f"/content/{_PIC_ID}", "format": "",
                       "category": "doginal", "tags": [], "uses": 0, "sourceName": "this arcade"} for n in range(8)]})))
    page.route("**/dogefeedx/api/launchpad", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"source": "arcade", "launches": [
            {"slug": "e" * 64, "name": "Much Dogs mint", "image": f"/content/{_PIC_ID}", "detail": "minting",
             "progress": "", "pct": "", "href": "/mintpad/" + "e" * 64}]})))


def test_market_window_is_dark_and_its_columns_do_not_cover_each_other(base):
    """The Market window on the DogeFeedX desktop: dark board, loaded thumbnails, no site nav."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _chrome(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _arcade_rows(page)
        page.goto(base + "/desktop")
        _wait_skin(page, "ubuntu")
        _open_system_themes(page)
        _pick_theme(page, "DogeFeedX")
        _wait_skin(page, "dogefeedx")
        page.locator('#dk-dock .ub-dockbtn[title="Market"]').click()
        page.wait_for_function("""() => [...document.querySelectorAll('.dk-win iframe')].some(f => {
          try { return f.contentWindow.location.pathname === '/dogefeedx/market'; } catch (e) { return false; }
        })""", timeout=20000)
        frame = next(f for f in page.frames if f.url.endswith("/dogefeedx/market"))
        frame.wait_for_selector(".df-collection img", timeout=25000)
        frame.wait_for_function("""() => {
          const img = document.querySelector('.df-collection img');
          return img && img.complete && img.naturalWidth > 0;
        }""", timeout=20000)
        report = frame.evaluate("""() => {
          const rgb = (s) => (s.match(/[\\d.]+/g) || []).slice(0, 3).map(Number);
          const lum = (s) => { const c = rgb(s); return c.length ? (c[0] + c[1] + c[2]) / 3 : 255; };
          const title = document.querySelector('h1');
          const img = document.querySelector('.df-collection img');
          return {
            indesk: document.documentElement.classList.contains('indesk'),
            body: lum(getComputedStyle(document.body).backgroundColor),
            title: lum(getComputedStyle(title).color),
            sidebar: !!document.querySelector('nav.df-sidebar, aside.df-right'),
            img: img.src
          };
        }""")
        assert report["indesk"] is True
        assert report["body"] < 40, report
        assert report["title"] > 180, report
        assert report["sidebar"] is False, report
        assert "/content/" in report["img"], report["img"]            # the arcade's own picture
        page.locator('.dk-win[aria-label="Market"]').screenshot(
            path=_shot("market-window-dfx.png"))
        browser.close()


def test_theme_buttons_on_your_arcade_show_inside_a_narrow_desktop_window(base):
    """A signed-in /me inside the desktop keeps its theme buttons even when that window is phone-narrow."""
    from playwright.sync_api import sync_playwright

    token = _session(base)
    with sync_playwright() as playwright:
        browser = _chrome(playwright)
        context = browser.new_context(
            viewport={"width": 520, "height": 800},
            screen={"width": 520, "height": 800})
        context.add_cookies([{
            "name": "arcade_session", "value": token, "url": base,
        }])
        page = context.new_page()
        page.goto(base + "/desktop")
        _wait_skin(page, "ubuntu")
        page.evaluate("arcadeShell.open('/me', 'Your arcade')")
        page.wait_for_function("""() => [...document.querySelectorAll('.dk-win iframe')].some(f => {
          try { return f.contentWindow.location.pathname === '/me'; } catch (e) { return false; }
        })""")
        report = page.evaluate("""() => {
          const f = [...document.querySelectorAll('.dk-win iframe')].find(f => f.contentWindow.location.pathname === '/me');
          const doc = f.contentDocument;
          const box = doc.getElementById('desk-theme-pref');
          return {
            hidden: box.hidden,
            labels: [...box.querySelectorAll('[data-desk-set]')].map(b => b.textContent.trim()),
            width: f.contentWindow.innerWidth,
            screen: f.contentWindow.screen.width,
            indesk: doc.documentElement.classList.contains('indesk')
          };
        }""")
        assert report["indesk"] is True
        assert max(report["width"], report["screen"]) <= 640, report
        assert report["hidden"] is False, report
        for need in ("Website", "Windows 98 desktop", "Ubuntu desktop", "DogeFeedX (macOS)"):
            assert need in report["labels"], report
        browser.close()


_DOCK = [
    ("Home", "/dogefeedx/feed", "What's the latest? (every post is a bark)"),
    ("Profile", "/dogefeedx/profile", "local-profile"),
    ("Files", "/dogefeedx/files", "Collections"),
    ("Notifications", "/dogefeedx/notifications", "Likes"),
    ("Messages", "/dogefeedx/messages", "New group"),
    ("Explore", "/dogefeedx/explore", "explore-kind"),
    ("Market", "/dogefeedx/market", "Market"),
    ("Inscribe", "/dogefeedx/inscribe", "/create/ord"),
    ("Wallet", "/dogefeedx/wallet", "Wallet"),
    ("Launchpad", "/dogefeedx/launchpad", "/mintpad/new"),
    ("NFTs", "/dogefeedx/nfts", "NFTs"),
    ("Arcade", "/dogefeedx/arcade", "Arcade"),
    ("Bookmarks", "/dogefeedx/bookmarks", "Bookmarks"),
    ("Meme Library", "/dogefeedx/memes", "Meme Library"),
    ("Leaderboard", "/dogefeedx/leaderboard", "Top referrers"),
    ("Settings", "/dogefeedx/settings", "Settings"),
]


def _scale(style):
    import re
    found = re.search(r"scale\(([\d.]+)\)", style or "")
    return float(found.group(1)) if found else 1.0


def test_the_dock_magnifies_shows_real_badges_and_opens_each_app_once(base):
    """macOS dock: the icon under the pointer grows, neighbours grow less, badges are real counts."""
    import json
    from playwright.sync_api import sync_playwright

    counts = {"now": {
        "/notifications": 3, "/me/notifications": 0,
        "/messages": 0, "/me/messages": 2, "/exchange": 1,
    }}

    with sync_playwright() as playwright:
        browser = _chrome(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _arcade_rows(page)
        page.add_init_script("localStorage.setItem('arcade.deskview','dogefeedx')")
        page.route("**/account/badges", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(counts["now"])))
        page.goto(base + "/desktop")
        _wait_skin(page, "dogefeedx")
        _deduped(page)
        strip = page.evaluate("""() => ({
          body: getComputedStyle(document.body).backgroundColor,
          desk: getComputedStyle(document.querySelector('.dk')).bottom,
          dock: getComputedStyle(document.querySelector('.dk-dock')).bottom
        })""")
        assert strip["body"] == "rgb(0, 0, 0)", strip
        assert strip["desk"] == "0px" and strip["dock"] == "0px", strip
        page.screenshot(path=_shot("desktop-bottom-dfx.png"))

        apps = page.locator("#dk-dock .dfx-dock-app")
        assert apps.count() == 16
        tops = page.evaluate("""() => [...document.querySelectorAll('#dk-dock .dfx-dock-app')]
          .map(b => b.getBoundingClientRect().top)""")
        assert max(tops) - min(tops) < 8, tops
        home = apps.nth(0)
        box = home.bounding_box()
        page.locator("#dk-dock").dispatch_event("mousemove", {
            "clientX": box["x"] + box["width"] / 2,
            "clientY": box["y"] + box["height"] / 2,
        })
        scales = page.evaluate("""() => [...document.querySelectorAll('#dk-dock .dfx-dock-app')]
          .map(b => b.style.transform)""")
        grown, neighbour, far = _scale(scales[0]), _scale(scales[1]), _scale(scales[-1])
        assert grown > neighbour > 1, scales
        assert far == 1, scales
        page.locator("#dk-dock").screenshot(path=_shot("dock-magnify-dfx.png"))

        def badge(title):
            return page.locator(f'#dk-dock .dfx-dock-app[title="{title}"] .dfx-badge')

        assert badge("Notifications").inner_text() == "3"
        assert badge("Messages").inner_text() == "2"
        assert badge("Market").inner_text() == "1"
        assert badge("Home").count() == 0

        counts["now"] = {key: 0 for key in counts["now"]}
        page.reload()
        _wait_skin(page, "dogefeedx")
        page.wait_for_function("() => document.querySelectorAll('#dk-dock .dfx-dock-app').length === 16")
        assert page.locator("#dk-dock .dfx-badge").count() == 0

        for title, path, needle in _DOCK:
            page.locator(f'#dk-dock .dfx-dock-app[title="{title}"]').click()
            win = page.locator(f'.dk-win:has(iframe[src="{path}"])')
            win.wait_for(timeout=20000)
            body = win.frame_locator("iframe").locator("body")
            body.wait_for(timeout=20000)
            page.wait_for_function(
                """(path) => [...document.querySelectorAll('.dk-win iframe')].some(f => {
                  try { return f.contentWindow.location.pathname === path; } catch (e) { return false; }
                })""",
                arg=path, timeout=20000)
            assert body.evaluate("() => location.pathname") == path, title
            html = body.inner_html()
            assert needle in html or needle in body.inner_text(), (title, needle)
            if title == "Files":
                body.get_by_text("does not ask for a key").wait_for(timeout=15000)
                html = body.inner_html()
                assert "/dogefeedx/api/wallet/holdings" in html and "DRC-20" in html and "Names" in html
                assert "/account/nfts/collections" not in html
            if title == "Meme Library":
                assert "Pick a picture" in html and "dogefeedx.memes" in html
                page.wait_for_function(
                    """() => {
                      const f = [...document.querySelectorAll('iframe')].find(el => (el.getAttribute('src') || '').endsWith('/dogefeedx/memes'));
                      const box = f && f.contentDocument && f.contentDocument.getElementById('memes');
                      if (!box || /Loading/.test(box.textContent)) return false;
                      const imgs = [...box.querySelectorAll('img')];
                      const cards = [...box.querySelectorAll('.card')];
                      if (!cards.length || cards.length !== imgs.length) return false;
                      const loaded = imgs.filter(i => i.complete && i.naturalWidth > 0);
                      return loaded.length >= 8 && imgs.every(i => (i.getAttribute('src') || '').indexOf('/content/') >= 0);
                    }""",
                    timeout=40000)
            if title == "Launchpad":
                page.wait_for_function(
                    """() => {
                      const f = [...document.querySelectorAll('iframe')].find(el => (el.getAttribute('src') || '').endsWith('/dogefeedx/launchpad'));
                      const box = f && f.contentDocument && f.contentDocument.getElementById('df-launches');
                      if (!box || /Loading/.test(box.textContent)) return false;
                      const imgs = [...box.querySelectorAll('img')];
                      return imgs.length > 0 && imgs.some(i => i.complete && i.naturalWidth > 0);
                    }""",
                    timeout=25000)
            if title == "Leaderboard":
                body.locator("#df-leaderboard .df-empty, #df-leaderboard a").first.wait_for(timeout=20000)
            if title == "NFTs":
                body.locator("#df-nfts .df-empty, #df-nfts .df-nft-card").first.wait_for(timeout=20000)
            if title in ("Files", "Notifications", "Messages", "Inscribe", "Wallet",
                         "Launchpad", "NFTs", "Bookmarks", "Meme Library", "Leaderboard", "Settings"):
                slug = title.lower().replace(" ", "-")
                win.screenshot(path=_shot(f"{slug}-window-dfx.png"))
            win.locator('[data-a="close"]').click()
            page.wait_for_function(
                """(path) => ![...document.querySelectorAll('.dk-win iframe')].some(f => f.getAttribute('src') === path)""",
                arg=path, timeout=10000)
        browser.close()
