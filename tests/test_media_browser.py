"""Camera & microphone (2026-10-22, the operator: a game card can be allowed the camera and microphone "in the same way that
they allow an app Wallet access"): two players in a fast room, in two real Chromiums with fake cameras. The viewer
opens the camera, nothing goes to anybody before the game shares it, a shared picture arrives and plays where the game
placed it, its sound stays silent until the game hears that player, the viewer's own Stop ends it (the game hears
'off' and the other side 'media-off'), and a page that is not allowed is told 'off'. Chromium and Firefox renegotiate
with each other too."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server, _mesh             # noqa: E402
from test_realtime_web import make_state                            # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright's Chromium")

GAME = "ce" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body style="margin:0">
<script src="/r/realtime.js"></script>
<script>
window.log = []; window.room = null; window.states = {}; window.has = {};
arcade.realtime.join('call', {game: 'media-test', fast: true}).then(function (r) {
  window.room = r;
  r.fast.on('peer', function (p, s) { window.states[p.id] = s; });
  r.media.on('media', function (p, h) { window.has[p.id] = h; window.log.push(['media', p.id, h]); });
  r.media.on('media-off', function (p) { delete window.has[p.id]; window.log.push(['media-off', p.id]); });
  r.media.on('state', function (s) { window.log.push(['state', s]); });
});
</script>"""

ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required"]


def _server(tmp_path):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.set_setting("realtime_public_stun", False)
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    return state, Server(create_app(state), port), f"http://127.0.0.1:{port}"


def _player(chrome, base, allowed=True):
    ctx = chrome.new_context(permissions=["camera", "microphone"])
    if allowed:
        ctx.add_init_script(f"try {{ localStorage.setItem('arcade.media.allow:{GAME}', 'yes'); }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(f"{base}/inscriptions/{GAME}/full")
    frame = page.frame_locator(".inscription-frame")
    return page, page.frames[1] if len(page.frames) > 1 else None, frame


def _game(page):
    for f in page.frames:
        if f != page.main_frame and "/content/" in (f.url or ""):
            return f
    raise AssertionError(f"no game frame in {[f.url for f in page.frames]}")


def _until(frame, js, what, timeout=30000):
    try:
        frame.wait_for_function(js, timeout=timeout)
    except Exception as exc:                       # noqa: BLE001
        raise AssertionError(f"timed out waiting for {what}: {exc}") from None


def _launch(pw):
    try:
        return pw.chromium.launch(args=ARGS)
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"no Chromium: {exc}")


def test_a_picture_goes_only_where_the_game_shares_it(tmp_path, no_nodes):
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            pb, _, _ = _player(chrome, base)
            ga, gb = _game(pa), _game(pb)
            _until(ga, "window.room && window.room.media && window.room.me", "a in")
            _until(gb, "window.room && window.room.media && window.room.me", "b in")
            ida, idb = ga.evaluate("window.room.me.id"), gb.evaluate("window.room.me.id")
            _until(ga, f"window.states[{idb!r}] === 'direct'", "a direct")
            _until(gb, f"window.states[{ida!r}] === 'direct'", "b direct")

            for g in (ga, gb):
                said = g.evaluate("window.room.media.start({video: true, audio: true})")
                assert said == {"ok": True, "video": True, "audio": True}, said
            ga.evaluate("window.room.media.place('me', {x: 5, y: 5, w: 80, h: 60}, {mirror: true})")
            _until(pa, "(() => { var v = document.querySelector('.arcade-media-layer video[data-media=me]');"
                       " return v && v.videoWidth > 0 && v.style.display === 'block' && v.muted; })()",
                   "a's own preview, silent")
            assert pa.evaluate("!document.querySelector('.arcade-media-pill').hidden"), "the pill shows"
            assert "Camera and microphone on" in pa.inner_text(".arcade-media-pill")

            # Started, but shared with nobody: b gets nothing.
            pa.wait_for_timeout(2500)
            assert gb.evaluate(f"!window.has[{ida!r}]"), "nothing before share"
            assert pb.evaluate(f"!document.querySelector('.arcade-media-layer video[data-media={ida!r}]')".replace(
                "data-media=" + repr(ida), 'data-media="' + ida + '"'))

            said = ga.evaluate(f"window.room.media.share({idb!r}, true)")
            assert said == {"ok": True}, said
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video && window.has[{ida!r}].audio",
                   "b hears a is sending")
            gb.evaluate(f"window.room.media.place({ida!r}, {{x: 0, y: 100, w: 320, h: 240}}, {{fit: 'contain'}})")
            tile = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            _until(pb, f"(() => {{ var v = {tile}; return v && v.videoWidth > 0 && v.currentTime > 0"
                       " && v.style.display === 'block'; })()", "a's picture playing on b")
            assert pb.evaluate(f"{tile}.muted"), "a's sound is silent until b's game hears a"
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            _until(pb, f"!{tile}.muted", "a's sound plays")
            box = pb.evaluate(f"(() => {{ var b = {tile}.getBoundingClientRect(),"
                              " f = document.querySelector('.inscription-frame').getBoundingClientRect();"
                              " return [b.left - f.left, b.top - f.top, b.width, b.height]; })()")
            assert [round(x) for x in box] == [0, 100, 320, 240], box
            assert gb.evaluate(f"!window.has[{idb!r}]") and ga.evaluate(f"!window.has[{idb!r}]"), \
                "b shared nothing: a gets nothing from b"

            # Muted: the track stays, its frames go dark; the pill says so when both are.
            ga.evaluate("window.room.media.mute({video: true, audio: true})")
            assert "(muted)" in pa.inner_text(".arcade-media-pill")
            ga.evaluate("window.room.media.mute({video: false, audio: false})")

            # The viewer's own Stop: a's game hears 'off', b's hears 'media-off', a's camera is closed.
            pa.click(".arcade-media-pill .arcade-media-stop")
            _until(ga, "window.log.some(function (e) { return e[0] === 'state' && e[1] === 'off'; })", "a told off")
            _until(gb, f"!window.has[{ida!r}]", "b told media-off", timeout=20000)
            assert pa.evaluate("document.querySelector('.arcade-media-pill').hidden")
            assert pa.evaluate("!document.querySelector('.arcade-media-layer video[data-media=me]')")
            # Started again and shared again: it comes back over the same link.
            ga.evaluate("window.room.media.start({video: true, audio: false})")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video && !window.has[{ida!r}].audio",
                   "video only, again")
            # Unshared: b hears media-off.
            ga.evaluate(f"window.room.media.share({idb!r}, false)")
            _until(gb, f"!window.has[{ida!r}]", "unshared", timeout=20000)
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_game_not_allowed_is_told_off_and_nothing_opens(tmp_path, no_nodes):
    """This page is no game (no card): the viewer asks, as Wallet access does for a shop; said no, the game hears
    'off' and the camera was never opened."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base, allowed=False)
            ga = _game(pa)
            _until(ga, "window.room && window.room.media && window.room.me", "in")
            ga.evaluate("window.said = null; window.room.media.start({video: true, audio: true})"
                        ".then(function (r) { window.said = r; }); 0")
            pa.wait_for_selector("text=Allow this app to use your camera and microphone?", timeout=10000)
            pa.get_by_role("button", name="Cancel").click()
            _until(ga, "window.said", "the answer")
            assert ga.evaluate("window.said") == {"ok": False, "error": "off"}
            assert pa.evaluate("!document.querySelector('.arcade-media-pill') "
                               "|| document.querySelector('.arcade-media-pill').hidden")
            assert pa.evaluate("localStorage.getItem('arcade.media.allow:' + %r)" % GAME) is None
            # Said yes: allowed from then on, and it opens.
            ga.evaluate("window.said = null; window.room.media.start({video: true, audio: true})"
                        ".then(function (r) { window.said = r; }); 0")
            pa.wait_for_selector("text=Allow this app to use your camera and microphone?", timeout=10000)
            pa.get_by_role("button", name="Allow camera & microphone").click()
            _until(ga, "window.said", "the answer")
            assert ga.evaluate("window.said")["ok"] is True
            assert pa.evaluate("localStorage.getItem('arcade.media.allow:' + %r)" % GAME) == "yes"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_chrome_and_firefox_share_pictures(tmp_path, no_nodes):
    """The renegotiation over the pair's own channel (whole descriptions, media sections added to a connection that
    started as data only) has to work between the two browsers both ways."""
    import os
    pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    def _browser():
        options = Options()
        options.add_argument("-headless")
        for pref, value in (("media.peerconnection.ice.obfuscate_host_addresses", False),
                            ("media.peerconnection.ice.loopback", True), ("media.navigator.streams.fake", True),
                            ("media.navigator.permission.disabled", True), ("media.autoplay.default", 0)):
            options.set_preference(pref, value)
        driver_path = os.environ.get("ARCADE_GECKODRIVER") or os.path.expanduser("~/opt/firefox/geckodriver-wrap")
        try:
            return webdriver.Firefox(options=options, service=Service(executable_path=driver_path)
                                     if os.access(driver_path, os.X_OK) else None)
        except Exception as exc:                   # noqa: BLE001
            pytest.skip(f"no usable browser: {exc}")

    state, server, base = _server(tmp_path)
    ff = None
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            ga = _game(pa)
            ff = _browser()
            ff.get(f"{base}/inscriptions/{GAME}/full")
            ff.execute_script(f"localStorage.setItem('arcade.media.allow:{GAME}', 'yes')")
            ff.switch_to.frame(ff.find_element(By.CSS_SELECTOR, ".inscription-frame"))
            _until(ga, "window.room && window.room.media && window.room.me", "chrome in")
            import time
            for _ in range(150):
                if ff.execute_script("return !!(window.room && window.room.media && window.room.me)"):
                    break
                time.sleep(0.2)
            ida, idb = ga.evaluate("window.room.me.id"), ff.execute_script("return window.room.me.id")
            _until(ga, f"window.states[{idb!r}] === 'direct'", "chrome direct", timeout=40000)
            assert ga.evaluate("window.room.media.start({video: true, audio: true})")["ok"]
            assert ga.evaluate(f"window.room.media.share({idb!r}, true)") == {"ok": True}
            for _ in range(150):
                if ff.execute_script(f"return !!(window.has[{ida!r}] && window.has[{ida!r}].video)"):
                    break
                time.sleep(0.2)
            assert ff.execute_script(f"return !!(window.has[{ida!r}] && window.has[{ida!r}].video)"), \
                "firefox got chrome's picture"
            # And back: firefox's fake camera to chrome (an offer from the other side of the pair).
            ff.execute_script("window.said = null; window.room.media.start({video: true, audio: false})"
                              ".then(function (r) { window.said = r; }); 0")
            for _ in range(100):
                if ff.execute_script("return window.said"):
                    break
                time.sleep(0.2)
            said = ff.execute_script("return window.said")
            if said and said.get("ok"):
                ff.execute_script(f"window.room.media.share({ida!r}, true)")
                _until(ga, f"window.has[{idb!r}] && window.has[{idb!r}].video", "chrome got firefox's picture",
                       timeout=30000)
            else:
                pytest.skip(f"this Firefox has no fake camera: {said}")
            chrome.close()
    finally:
        if ff is not None:
            ff.quit()
        server.stop()
        state.mesh.stop()


def test_voice_only_then_the_camera_joins_mid_call(tmp_path, no_nodes):
    """2026-10-22: "If somebody doesn't want to share their video, they just want to share their audio, that's
    fine"; and a call of up to 20 sends every voice to everybody but each picture to a few (share by kind)."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            pb, _, _ = _player(chrome, base)
            ga, gb = _game(pa), _game(pb)
            _until(ga, "window.room && window.room.media && window.room.me", "a in")
            _until(gb, "window.room && window.room.media && window.room.me", "b in")
            ida, idb = ga.evaluate("window.room.me.id"), gb.evaluate("window.room.me.id")
            _until(ga, f"window.states[{idb!r}] === 'direct'", "a direct")
            assert ga.evaluate("window.room.media.start({video: false, audio: true})") == \
                {"ok": True, "video": False, "audio": True}
            assert "Microphone on" in pa.inner_text(".arcade-media-pill")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio && !window.has[{ida!r}].video", "voice only")
            # The camera joins: a's voice never stops on b (no media-off between).
            assert ga.evaluate("window.room.media.start({video: true, audio: true})")["video"] is True
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video && window.has[{ida!r}].audio", "both")
            assert not gb.evaluate("window.log.some(function (e) { return e[0] === 'media-off'; })"), \
                "the voice went on while the camera joined"
            # By kind: the voice still goes, the picture no longer.
            ga.evaluate(f"window.room.media.share({idb!r}, {{video: false, audio: true}})")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio && !window.has[{ida!r}].video",
                   "the picture stops, the voice goes on", timeout=20000)
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_ring_and_ringed_answer_in_the_viewer(tmp_path, no_nodes):
    """arcade.realtime.ring / ringed (arcade/ring.py) reach the viewer and come back: a page opened without a
    notification's ring has none, and a guest cannot ring anybody."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            ga = _game(pa)
            _until(ga, "window.arcade && window.arcade.realtime && window.arcade.realtime.ringed", "loaded")
            assert ga.evaluate("arcade.realtime.ringed()") is None
            said = ga.evaluate("arcade.realtime.ring('nSomebody', {line: 'hi'})")
            assert said["ok"] is False and "sign in" in said["error"], said
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()
