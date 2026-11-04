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
  window.speak = {}; r.media.on('speaking', function (p, on) { window.speak[p.id] = on; window.log.push(['speaking', p.id, on]); });
  window.pics = []; r.media.on('frame', function (key, bm, t) { window.pics.push([key, bm.width, bm.height, t]); bm.close(); });
  window.lv = null; r.media.on('levels', function (by) { window.lv = by; });
  r.on('reconnecting', function () { window.log.push(['reconnecting']); });
  r.on('rejoined', function (me, media) { window.log.push(['rejoined', media]); });
});
</script>"""

ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--auto-accept-this-tab-capture",
        "--auto-select-desktop-capture-source=Entire screen",
        "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required"]


def _server(tmp_path):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.set_setting("realtime_public_stun", False)
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    return state, Server(create_app(state), port), f"http://127.0.0.1:{port}"


def _player(chrome, base, allowed=True, pictures=False):
    ctx = chrome.new_context(permissions=["camera", "microphone"])
    if allowed:
        ctx.add_init_script(f"try {{ localStorage.setItem('arcade.media.allow:{GAME}', 'yes'); }} catch (e) {{}}")
    if pictures:
        ctx.add_init_script(f"try {{ localStorage.setItem('arcade.pictures.allow:{GAME}', 'yes'); }} catch (e) {{}}")
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
                assert said == {"ok": True, "video": True, "audio": True, "screen": False}, said
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


def test_a_camera_stops_when_its_permission_is_turned_off_elsewhere(tmp_path, no_nodes):
    """2026-11-01: off in the app's Permissions window, Remove from arcade or Game access is another page writing the
    same switch; the camera already running goes with it, and the other player is told."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            ga = _game(pa)
            _until(ga, "window.room && window.room.media && window.room.me", "a in")
            said = ga.evaluate("window.room.media.start({video: true, audio: true})")
            assert said["ok"], said
            pa.wait_for_function("!document.querySelector('.arcade-media-pill').hidden")
            other = pa.context.new_page()
            other.goto(f"{base}/feed")
            other.evaluate(f"localStorage.removeItem('arcade.media.allow:{GAME}')")
            _until(ga, "window.log.some(function (e) { return e[0] === 'state' && e[1] === 'off'; })", "a told off")
            pa.wait_for_function("document.querySelector('.arcade-media-pill').hidden")
            chrome.close()
    finally:
        server.stop()


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
                {"ok": True, "video": False, "audio": True, "screen": False}
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


def test_a_screen_goes_with_the_voice_and_the_browser_s_stop_keeps_the_voice(tmp_path, no_nodes):
    """Screen Share (2026-10-29: "share the view of their desktop and allows audio communication"): start({screen,
    audio}) sends the screen as the picture with the microphone; the browser's own Stop sharing ends the picture only
    (the game hears 'screen-off', the other side keeps the voice); start({audio}) after a screen keeps the voice too."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            pb, _, _ = _player(chrome, base)
            ga, gb = _game(pa), _game(pb)
            _until(ga, "window.room && window.room.media && window.room.me", "a in")
            _until(gb, "window.room && window.room.media && window.room.me", "b in")
            assert ga.evaluate("window.room.media.screen") is True, "a computer's browser can share its screen"
            ida, idb = ga.evaluate("window.room.me.id"), gb.evaluate("window.room.me.id")
            _until(ga, f"window.states[{idb!r}] === 'direct'", "a direct")
            said = ga.evaluate("window.room.media.start({screen: true, audio: true})")
            assert said == {"ok": True, "video": True, "audio": True, "screen": True}, said
            assert "Sharing your screen, microphone on" in pa.inner_text(".arcade-media-pill")
            assert pa.evaluate("(() => { const t = document.querySelector('.arcade-media-layer') && "
                               "[...document.querySelectorAll('.arcade-media-layer video')]; return true; })()")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video && window.has[{ida!r}].audio",
                   "b gets a's screen and voice")
            gb.evaluate(f"window.room.media.place({ida!r}, {{x: 0, y: 0, w: 640, h: 360}}, {{fit: 'contain'}})")
            tile = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            _until(pb, f"(() => {{ var v = {tile}; return v && v.videoWidth > 0 && v.currentTime > 0; }})()",
                   "a's screen playing on b")
            # The browser's own Stop sharing (the track ends): the picture goes, the voice stays.
            pa.evaluate("""() => { for (const v of document.querySelectorAll('.arcade-media-layer video'))
                if (v.srcObject) v.srcObject.getVideoTracks().forEach(t => { t.stop(); t.dispatchEvent(new Event('ended')); }); }""")
            _until(ga, "window.log.some(function (e) { return e[0] === 'state' && e[1] === 'screen-off'; })",
                   "a's game told screen-off")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio && !window.has[{ida!r}].video",
                   "b keeps a's voice, without the picture", timeout=20000)
            assert "Microphone on" in pa.inner_text(".arcade-media-pill")
            # Shared again, then back to the voice alone by start({audio}).
            assert ga.evaluate("window.room.media.start({screen: true, audio: true})")["screen"] is True
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video", "the screen again")
            assert ga.evaluate("window.room.media.start({audio: true})") == \
                {"ok": True, "video": False, "audio": True, "screen": False}
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio && !window.has[{ida!r}].video",
                   "voice alone again", timeout=20000)
            assert not gb.evaluate("window.log.some(function (e) { return e[0] === 'media-off'; })"), \
                "the voice never stopped"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


FAKE_SCREEN = """(() => { const real = navigator.mediaDevices.getDisplayMedia ? navigator.mediaDevices : null;
  navigator.mediaDevices.getDisplayMedia = async (c) => { window.__asked = c;
    const v = document.createElement('canvas'); v.width = 320; v.height = 180; const x = v.getContext('2d');
    setInterval(() => { x.fillStyle = '#' + Math.floor(Math.random() * 4095).toString(16).padStart(3, '0'); x.fillRect(0, 0, 320, 180); }, 100);
    const s = v.captureStream(10);
    if (c.audio) { const a = new AudioContext(); await a.resume(); const o = a.createOscillator(); o.frequency.value = 880; const d = a.createMediaStreamDestination(); o.connect(d); o.start(); s.addTrack(d.stream.getAudioTracks()[0]); window.__tone = o; }
    return s; }; })()"""
LOUD = """(id) => new Promise((ok) => { const v = document.querySelector(`.arcade-media-layer video[data-media="${id}"]`);
  const a = new AudioContext(); a.resume(); const n = a.createAnalyser(); n.fftSize = 2048; a.createMediaStreamSource(v.srcObject).connect(n);
  const b = new Float32Array(1024); let top = 0, i = 0;
  const t = setInterval(() => { n.getFloatTimeDomainData(b); for (const x of b) top = Math.max(top, Math.abs(x)); if (++i > 14) { clearInterval(t); a.close(); ok(top); } }, 100); })"""


def test_a_screen_s_sound_goes_with_it_and_each_player_has_their_own_volume(tmp_path, no_nodes):
    """2026-11-01. One audio track, mixed: muting the microphone leaves what is shared playing. An app that does not ask gets no sound."""
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
            pa.evaluate(FAKE_SCREEN)
            said = ga.evaluate("window.room.media.start({screen: true, audio: true})")
            assert "sound" not in said and not pa.evaluate("window.__asked.audio"), "a page that did not ask gets none"
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            said = ga.evaluate("window.room.media.start({screen: true, audio: true, sound: true})")
            assert said.get("sound") is True, said
            assert "Sharing your screen and its sound" in pa.inner_text(".arcade-media-pill")
            ga.evaluate("window.room.media.mute({audio: true})")                       # the microphone only
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio", "b hears a")
            pb.wait_for_timeout(1500)
            assert pb.evaluate(LOUD, ida) > .01, "what the screen plays arrives with the microphone muted"
            tracks = pa.evaluate("document.querySelector('.arcade-media-layer video[data-media=me]').srcObject.getAudioTracks().length")
            assert tracks == 1, "one track, mixed"
            tile = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            assert pb.evaluate(tile + ".volume") == 1
            gb.evaluate(f"window.room.media.volume({ida!r}, 0.25)")
            assert pb.evaluate(tile + ".volume") == .25
            gb.evaluate(f"window.room.media.volume({ida!r}, 7)")
            assert pb.evaluate(tile + ".volume") == 1 and pb.evaluate(tile + ".muted"), "past 1 the element stays at 1 and the sound goes through a gain"
            ga.evaluate("window.room.media.stop()")
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_screen_goes_beside_the_camera_as_a_second_picture_with_its_own_sound(tmp_path, no_nodes):
    """2026-11-01: the camera stays, the screen is a new box. Stopping the screen leaves the camera, and a page that never asks sees nothing new."""
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
            pa.evaluate(FAKE_SCREEN)
            assert ga.evaluate("window.room.media.start({video: true, audio: true})")["video"] is True
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            gb.evaluate(f"window.room.media.place({ida!r}, {{x: 0, y: 0, w: 320, h: 180}})")
            cam = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            scr = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}#screen\"]')"
            _until(pb, f"(() => {{ var v = {cam}; return v && v.videoWidth > 0; }})()", "a's camera")
            said = ga.evaluate("window.room.media.showScreen({sound: true})")
            assert said == {"ok": True, "sound": True}, said
            gb.evaluate(f"window.room.media.place({ida!r} + '#screen', {{x: 0, y: 200, w: 320, h: 180}}, {{fit: 'contain'}})")
            ga.evaluate("window.room.media.place('me#screen', {x: 0, y: 0, w: 160, h: 90})")
            _until(pb, f"(() => {{ var v = {scr}; return v && v.videoWidth > 0 && v.style.display !== 'none'; }})()", "a's screen, a second picture")
            assert pb.evaluate(f"{cam}.videoWidth > 0"), "the camera goes on"
            assert pb.evaluate(f"{cam}.srcObject.getVideoTracks().length") == 1
            assert "Sharing your screen and its sound" in pa.inner_text(".arcade-media-pill")
            pb.wait_for_timeout(1200)
            assert pb.evaluate(f"(id) => ({LOUD})(id)", ida + "#screen") > .01, "the screen has a sound of its own"
            gb.evaluate(f"window.room.media.volume({ida!r} + '#screen', 0.25)")
            assert pb.evaluate(f"{scr}.volume") == .25 and pb.evaluate(f"{cam}.volume") == 1, "and its own volume"
            # The browser's own Stop sharing: the screen goes, the camera stays.
            pa.evaluate("""() => { const v = document.querySelector('.arcade-media-layer video[data-media="me#screen"]');
                v.srcObject.getVideoTracks().forEach(t => { t.stop(); t.dispatchEvent(new Event('ended')); }); }""")
            _until(ga, "window.log.some(function (e) { return e[0] === 'state' && e[1] === 'screen-off'; })", "told screen-off")
            _until(pb, f"!{scr}", "the second picture gone", timeout=20000)
            assert pb.evaluate(f"{cam}.videoWidth > 0")
            assert not gb.evaluate("window.log.some(function (e) { return e[0] === 'media-off'; })"), "they never went away"
            assert ga.evaluate("window.room.media.showScreen({sound: true})")["ok"], "and again"
            ga.evaluate("window.room.media.hideScreen()")
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


#: A room in a page: music from the "speakers" reaches the "microphone" 60 ms later at half strength, over a steady 3 kHz "voice". The shared
#: screen's sound is the music itself. Every track is made here, so the test can read what comes out at the other player.
ROOM = """() => {
  const q = window.__q = new AudioContext(), rate = q.sampleRate, b = q.createBuffer(1, rate * 6, rate), d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * .5;
  const music = q.createBufferSource(); music.buffer = b; music.loop = true;
  const low = q.createBiquadFilter(); low.type = 'lowpass'; low.frequency.value = 1500; music.connect(low); music.start();
  const screen = q.createMediaStreamDestination(), mic = q.createMediaStreamDestination(); low.connect(screen);
  const late = q.createDelay(1), half = q.createGain(); late.delayTime.value = .06; half.gain.value = .5; low.connect(late); late.connect(half); half.connect(mic);
  const voice = q.createOscillator(), soft = q.createGain(); voice.frequency.value = 3000; soft.gain.value = .05; voice.connect(soft); soft.connect(mic); voice.start();
  q.resume();
  navigator.mediaDevices.getUserMedia = async (c) => { window.__mic = c; return new MediaStream([mic.stream.getAudioTracks()[0].clone()]); };
  navigator.mediaDevices.getDisplayMedia = async (c) => { window.__asked = c;
    const v = document.createElement('canvas'); v.width = 320; v.height = 180; const x = v.getContext('2d');
    setInterval(() => { x.fillRect(Math.random() * 300, 0, 9, 180); }, 100); const s = v.captureStream(10);
    s.addTrack(screen.stream.getAudioTracks()[0].clone()); return s; };
}"""

#: What reaches this ear, in two bands, in dB: [the music's, the voice's]. Averaged over a second.
EARS = """(id) => new Promise((done) => {
  const v = document.querySelector('.arcade-media-layer video[data-media="' + id + '"]'), q = window.__ear || (window.__ear = new AudioContext()); q.resume();
  const a = q.createAnalyser(); a.fftSize = 2048; a.smoothingTimeConstant = 0; q.createMediaStreamSource(new MediaStream(v.srcObject.getAudioTracks())).connect(a);
  const f = new Float32Array(a.frequencyBinCount), hz = q.sampleRate / a.fftSize, sum = [0, 0]; let n = 0;
  const t = setInterval(() => { a.getFloatFrequencyData(f); let m = 0, w = 0;
    for (let i = 0; i < f.length; i++) { const p = Math.pow(10, f[i] / 10), at = i * hz; if (at > 200 && at < 1200) m += p; if (at > 2900 && at < 3100) w += p; }
    sum[0] += m; sum[1] += w; n++;
    if (n === 40) { clearInterval(t); a.disconnect(); done(sum.map((x) => 10 * Math.log10(x / n + 1e-20))); } }, 25);
})"""


def test_what_the_speakers_play_is_taken_out_of_the_microphone_and_the_call_stays_out_of_the_screen(tmp_path, no_nodes):
    """2026-11-02. The screen's sound is what the speakers play, so it is taken out of what the microphone sends; the voice stays. Only while it is shared."""
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
            errors = []; pa.on("console", lambda m: errors.append(m.text) if m.type in ("warning", "error") else None)
            pa.evaluate(ROOM)
            ga.evaluate("window.room.media.start({audio: true})")
            assert pa.evaluate("window.__mic.audio.echoCancellation") is True, "the browser's own, not 'all' (lossy live)"
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio", "b hears a")
            pb.wait_for_timeout(1500)
            bare = pb.evaluate(EARS, ida)
            said = ga.evaluate("window.room.media.showScreen({sound: true})")
            assert said == {"ok": True, "sound": True}, said
            if pa.evaluate("!!navigator.mediaDevices.getSupportedConstraints().restrictOwnAudio"):
                assert pa.evaluate("window.__asked.audio.restrictOwnAudio") is True, "the call does not go out in the screen's sound"
            assert pa.evaluate("window.__asked.audio.echoCancellation") is False, "and music is no voice"
            pb.wait_for_timeout(9000)
            clean = pb.evaluate(EARS, ida)
            print("bare", bare, "clean", clean)
            assert bare[0] - clean[0] > 20, (bare, clean, errors)
            assert abs(bare[1] - clean[1]) < 2, ("the voice stays", bare, clean)
            assert pb.evaluate(f"(id) => ({LOUD})(id)", ida + "#screen") > .005, "and the screen's own sound goes on"
            # Muted for the others: the speakers go on, so the microphone stays clean.
            ga.evaluate("window.room.media.mute({sound: true})")
            assert "its sound muted" in pa.inner_text(".arcade-media-pill")
            pb.wait_for_timeout(3000)
            assert pb.evaluate(f"(id) => ({LOUD})(id)", ida + "#screen") < .002, "muted for the others"
            quiet = pb.evaluate(EARS, ida)
            assert bare[0] - quiet[0] > 20, (bare, quiet)
            ga.evaluate("window.room.media.mute({sound: false})")
            ga.evaluate("window.room.media.hideScreen()")
            pb.wait_for_timeout(1500)
            back = pb.evaluate(EARS, ida)
            assert abs(bare[0] - back[0]) < 4, ("no screen: the bare microphone again, on the same link", bare, back)
            ga.evaluate("window.room.media.stop()")
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()



def test_a_lounge_screen_shared_with_its_sound_has_the_echo_taken_out_of_the_microphone_in_the_mix(tmp_path, no_nodes):
    """Riffs 2026-11-03 (the operator, live: "both audios are being transmitted at one time"): a screen started with the microphone, its
    sound going in ONE track with it (mediaMix), never had the canceller. Now it goes between the microphone and the mix, the screen's
    sound its reference, and the voice still arrives."""
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
            pa.evaluate(ROOM)
            pa.evaluate("""() => { const W = window.AudioWorkletNode; window.__nodes = [];
                window.AudioWorkletNode = function (ctx, name, o) { window.__nodes.push(name); return new W(ctx, name, o); };
                window.AudioWorkletNode.prototype = W.prototype; }""")
            said = ga.evaluate("window.room.media.start({audio: true, screen: true, sound: true})")
            assert said.get("ok") and said.get("sound"), said
            _until(pa, "window.__nodes.indexOf('arcade-echo') >= 0", "the canceller in the mix", timeout=10000)
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio", "b hears a")
            pb.wait_for_timeout(3000)
            music, voice = pb.evaluate(EARS, ida)
            assert voice > -80 and music > -80, ("the voice and the screen both arrive", music, voice)
            ga.evaluate("window.room.media.stop()")
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()

def test_a_player_can_be_louder_than_they_send_and_it_never_crackles(tmp_path, no_nodes):
    """2026-11-02: past 100%. An element stops at 1, so a gain and a limiter. Read at the speakers: what the page sends to its audio
    destination -- since 2026-11-03 the loopback's, so that the echo cancelling hears it."""
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
            pa.evaluate(ROOM)
            pa.evaluate("""() => { const q = window.__q, o = q.createOscillator(), g = window.__level = q.createGain(), d = q.createMediaStreamDestination(); g.gain.value = .02; o.connect(g); g.connect(d); o.start();
                navigator.mediaDevices.getUserMedia = async () => new MediaStream([d.stream.getAudioTracks()[0].clone()]); }""")
            pb.evaluate("""() => { const was = AudioNode.prototype.connect; window.__taps = [];
                AudioNode.prototype.connect = function (to, ...r) { if (to instanceof AudioDestinationNode || to instanceof MediaStreamAudioDestinationNode) { const a = this.context.createAnalyser(); a.fftSize = 2048; was.call(this, a); window.__taps.push(a); if (to instanceof AudioDestinationNode) window.__direct = (window.__direct || 0) + 1; } return was.call(this, to, ...r); };
                const play = HTMLMediaElement.prototype.play; window.__plays = [];
                HTMLMediaElement.prototype.play = function () { window.__plays.push(this); return play.call(this); };
                window.__peak = () => { const a = window.__taps[window.__taps.length - 1], f = new Float32Array(2048); a.getFloatTimeDomainData(f); return Math.max(...f.map(Math.abs)); }; }""")
            ga.evaluate("window.room.media.start({audio: true})")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio", "b hears a")
            tile = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            gb.evaluate(f"window.room.media.volume({ida!r}, 1.01)")
            pb.wait_for_timeout(1500)
            assert pb.evaluate(tile + ".muted"), "one way out only, or it plays twice"
            # 2026-11-03 (Spatial World echo): the gain's output plays through a loopback into an <audio> element, which Chrome's
            # echo cancelling hears, never straight to the AudioContext's speakers, which it does not.
            pb.wait_for_timeout(1500)
            assert not pb.evaluate("window.__direct || 0"), "nothing straight to the speakers"
            assert pb.evaluate("window.__plays.some(e => e.tagName === 'AUDIO' && !e.muted && !e.paused && e.srcObject)"), "the loopback plays"
            low = pb.evaluate("window.__peak()")
            gb.evaluate(f"window.room.media.volume({ida!r}, 3)")
            pb.wait_for_timeout(600)
            high = pb.evaluate("window.__peak()")
            assert 2.4 < high / low < 3.6, (low, high)
            pa.evaluate("window.__level.gain.value = .9")
            gb.evaluate(f"window.room.media.volume({ida!r}, 4)")
            pb.wait_for_timeout(1500)
            assert pb.evaluate("window.__peak()") < 1.25, "held, not clipped"
            gb.evaluate(f"window.room.media.volume({ida!r}, 0.5)")
            assert pb.evaluate(tile + ".volume") == .5 and not pb.evaluate(tile + ".muted"), "and back to the plain way"
            ga.evaluate("window.room.media.stop()")
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_page_can_ask_how_each_link_really_goes(tmp_path, no_nodes):
    """2026-11-01: direct, relay, Cloudflare relay or not connected. What the browser reports, including a relay's url."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            pb, _, _ = _player(chrome, base)
            ga, gb = _game(pa), _game(pb)
            _until(ga, "window.room && window.room.me", "a in")
            _until(gb, "window.room && window.room.me", "b in")
            idb = gb.evaluate("window.room.me.id")
            _until(ga, f"window.states[{idb!r}] === 'direct'", "a direct")
            assert ga.evaluate("window.room.fast.routes()") == {idb: "direct"}, "two browsers on one machine"
            fake = """(c) => { RTCPeerConnection.prototype.getStats = async () => new Map([['t', {id: 't', type: 'transport', selectedCandidatePairId: 'p'}],
                ['p', {id: 'p', type: 'candidate-pair', localCandidateId: 'l', remoteCandidateId: 'r'}],
                ['l', Object.assign({id: 'l', type: 'local-candidate'}, c.near)], ['r', Object.assign({id: 'r', type: 'remote-candidate'}, c.far)]]); }"""
            cases = [({"candidateType": "relay", "url": "turn:156.67.28.112:3478?transport=udp"}, {"candidateType": "host"}, "relay"),
                     ({"candidateType": "relay", "url": "turn:turn.cloudflare.com:3478?transport=udp"}, {"candidateType": "srflx"}, "cloudflare"),
                     ({"candidateType": "srflx"}, {"candidateType": "relay"}, "relay"),
                     ({"candidateType": "prflx"}, {"candidateType": "srflx"}, "direct")]
            for near, far, want in cases:
                pa.evaluate(fake, {"near": near, "far": far})
                assert ga.evaluate("window.room.fast.routes()") == {idb: want}, (near, far)
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_relay_login_is_asked_for_again_once_it_has_grown_old(tmp_path, no_nodes):
    """A login lasts 6 hours and a page can stay open for days: it must not keep offering a dead one."""
    source = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates" / "_page_doors.js.html"
    text = source.read_text()
    assert "ICE_KEEP = 2 * 3600 * 1000" in text and "if (Date.now() - fastIceAt > ICE_KEEP) fastIce = null;" in text
    from arcade.mesh import turn
    assert turn.LOGIN_TTL >= 2 * 2 * 3600, "kept for less than half a login"


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


def test_a_rejoin_keeps_the_camera_the_screen_and_the_shares(tmp_path, no_nodes):
    """2026-11-02, a Riffs Lounge: every time the page took its seat again, the camera, the microphone and a shared screen
    were closed, and a screen cannot come back without the player's press. Now only the links start again: the pictures
    come back on the new links by themselves, with what the game shared, and the game is told what stayed open."""

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
            pa.evaluate(FAKE_SCREEN)
            assert ga.evaluate("window.room.media.start({video: true, audio: true})")["video"] is True
            assert ga.evaluate("window.room.media.showScreen({sound: true})")["ok"]
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.place({ida!r}, {{x: 0, y: 0, w: 320, h: 180}})")
            gb.evaluate(f"window.room.media.place({ida!r} + '#screen', {{x: 0, y: 200, w: 320, h: 180}})")
            cam = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}\"]')"
            scr = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}#screen\"]')"
            _until(pb, f"(() => {{ var v = {cam}; return v && v.videoWidth > 0; }})()", "a's camera")
            _until(pb, f"(() => {{ var v = {scr}; return v && v.videoWidth > 0; }})()", "a's screen")

            # One send that fails: a takes its seat again (b stays, so this is a's rejoin alone: when b comes back too,
            # a's game is told b left and must share with them again, which is right -- a guest's name can change hands).
            once = []
            def fail_once(route):
                if once:
                    route.continue_()
                else:
                    once.append(1)
                    route.abort()
            pa.route("**/realtime/send", fail_once)
            ga.evaluate("window.room.send({hello: 1}).catch(function () {}); 0")
            _until(ga, "window.log.some(function (e) { return e[0] === 'reconnecting'; })", "a reconnecting", timeout=60000)
            _until(ga, "window.log.some(function (e) { return e[0] === 'rejoined'; })", "a rejoined", timeout=60000)
            kept = ga.evaluate("window.log.filter(function (e) { return e[0] === 'rejoined'; })[0][1]")
            assert kept == {"video": True, "audio": True, "screen": True}, kept
            _until(ga, "window.room.online && window.room.fast", "a back online")
            _until(gb, "window.room.online", "b back online")
            ida2 = ga.evaluate("window.room.me.id")
            assert ida2 == ida, "a guest keeps its name"

            # Back on the new link, with nothing asked again: b's game placed them before, and a's shares were kept.
            _until(gb, f"window.states[{ida!r}] === 'direct'", "relinked", timeout=40000)
            _until(pb, f"(() => {{ var v = {cam}; return v && v.videoWidth > 0 && v.currentTime > 0; }})()",
                   "a's camera back on b", timeout=30000)
            _until(pb, f"(() => {{ var v = {scr}; return v && v.videoWidth > 0; }})()", "a's screen back on b", timeout=30000)
            log = ga.evaluate("window.log")
            assert not [e for e in log if e[0] == "state" and e[1] in ("off", "screen-off")], log
            assert "Sharing your screen" in pa.inner_text(".arcade-media-pill"), "still on air"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_chrome_and_firefox_offer_at_the_same_moment(tmp_path, no_nodes):
    """2026-11-02, a Riffs Lounge, Firefox and Chrome: a link came up with the camera on at both ends, both offered in the
    same instant, and Chrome, giving way, refused Firefox's offer ("RTP extension ID reassignment not supported"): no
    picture either way, ever. Reproduced every time before the fix. Run until both browsers have been the one giving way."""
    import os
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
    seen = set()
    for _ in range(8):
        if seen == {True, False}:
            break
        run = tmp_path / f"run{_}"
        run.mkdir()
        state, server, base = _server(run)
        try:
            with sync_api.sync_playwright() as pw:
                chrome = _launch(pw)
                try:
                    ff = pw.firefox.launch(firefox_user_prefs={
                        "media.navigator.streams.fake": True, "media.navigator.permission.disabled": True,
                        "media.peerconnection.ice.obfuscate_host_addresses": False,
                        "media.peerconnection.ice.loopback": True, "media.autoplay.default": 0})
                except Exception as exc:           # noqa: BLE001
                    pytest.skip(f"no Firefox for playwright: {exc}")
                refused = []
                pages = []
                for br in (chrome, ff):
                    ctx = br.new_context(permissions=["camera", "microphone"] if br is chrome else [])
                    ctx.add_init_script(f"try {{ localStorage.setItem('arcade.media.allow:{GAME}', 'yes'); }} catch (e) {{}}")
                    pg = ctx.new_page()
                    pg.on("console", lambda m: "was refused" in m.text and refused.append(m.text))
                    pg.goto(f"{base}/inscriptions/{GAME}/full")
                    pages.append(pg)
                gc, gf = _game(pages[0]), _game(pages[1])
                for g in (gc, gf):
                    _until(g, "window.room && window.room.media && window.room.me", "in", timeout=40000)
                idc, idf = gc.evaluate("window.room.me.id"), gf.evaluate("window.room.me.id")
                for g in (gc, gf):
                    assert g.evaluate("window.room.media.start({video: true, audio: true})")["ok"]
                _until(gc, f"window.states[{idf!r}] === 'direct'", "chrome direct", timeout=40000)
                _until(gf, f"window.states[{idc!r}] === 'direct'", "firefox direct", timeout=40000)
                gc.evaluate(f"window.room.media.share({idf!r}, true); 0")
                gf.evaluate(f"window.room.media.share({idc!r}, true); 0")
                _until(gc, f"window.has[{idf!r}] && window.has[{idf!r}].video", "chrome got firefox's picture", timeout=30000)
                _until(gf, f"window.has[{idc!r}] && window.has[{idc!r}].video", "firefox got chrome's picture", timeout=30000)
                assert not refused, refused
                seen.add(idc > idf)                    # True: Chrome was the one giving way
                ff.close()
                chrome.close()
        finally:
            server.stop()
            state.mesh.stop()
    assert seen == {True, False}, f"only saw chrome giving way = {seen}"


def test_muting_the_microphone_leaves_the_screens_sound(tmp_path, no_nodes):
    """2026-11-02, the operator: "When a user mute their own mic, it also mute the audio from their screen share they should be
    separate". Measured where it counts, on the other side: the screen's sound before and after the microphone is muted."""
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
            pa.evaluate(FAKE_SCREEN)
            assert ga.evaluate("window.room.media.start({video: true, audio: true})")["ok"]
            assert ga.evaluate("window.room.media.showScreen({sound: true})")["ok"]
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            gb.evaluate(f"window.room.media.place({ida!r}, {{x: 0, y: 0, w: 320, h: 180}})")
            gb.evaluate(f"window.room.media.place({ida!r} + '#screen', {{x: 0, y: 200, w: 320, h: 180}})")
            scr = f"document.querySelector('.arcade-media-layer video[data-media=\"{ida}#screen\"]')"
            _until(pb, f"(() => {{ var v = {scr}; return v && v.videoWidth > 0; }})()", "a's screen")
            pb.wait_for_timeout(1500)
            before = pb.evaluate(f"(id) => ({LOUD})(id)", ida + "#screen")
            assert before > .01, before
            ga.evaluate("window.room.media.mute({audio: true, video: false})")
            pb.wait_for_timeout(2500)
            after = pb.evaluate(f"(id) => ({LOUD})(id)", ida + "#screen")
            print("screen sound on b before", before, "after the mic muted", after)
            assert after > before * 0.5, f"the screen's sound went with the microphone: {before} -> {after}"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_who_is_speaking_is_said_to_the_page_and_stops_when_they_mute(tmp_path, no_nodes):
    """2026-11-02, the operator: green boxes round whoever is talking in a Riffs Lounge. The viewer says 'speaking' on and
    off for each player it hears (their voice, not their screen's sound) and for this player's own microphone."""
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
            ga.evaluate("window.room.media.start({video: false, audio: true})")
            gb.evaluate("window.room.media.start({video: false, audio: false})")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].audio", "b gets a's voice")
            assert not gb.evaluate(f"!!window.speak[{ida!r}]"), "not before b hears a"
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            # Chromium's fake microphone beeps: a voice to the meter.
            _until(gb, f"window.speak[{ida!r}] === true", "b sees a speaking", timeout=20000)
            _until(ga, f"window.speak[{ida!r}] === true", "a sees itself speaking", timeout=20000)
            ga.evaluate("window.room.media.mute({audio: true, video: false})")
            _until(ga, f"window.speak[{ida!r}] === false", "a's own box goes quiet on mute", timeout=10000)
            _until(gb, f"window.speak[{ida!r}] === false", "b sees a go quiet", timeout=15000)
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_pictures_go_to_the_app_as_bitmaps_only_with_its_switch_and_their_sound_is_placed_in_its_scene(tmp_path, no_nodes):
    """2026-11-02, Spatial World: "hand the video to the app, behind its own card permission". With Pictures in
    the app on, room.media.frames hands the app a player's picture as ImageBitmaps inside the size it asked for, the
    pill says so, and that player's sound goes the placed way (spatial, held back by the pictures' lag). Without the
    switch: 'off', and no picture reaches the app."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            pa, _, _ = _player(chrome, base)
            pb, _, _ = _player(chrome, base, pictures=True)
            pc, _, _ = _player(chrome, base)
            ga, gb, gc = _game(pa), _game(pb), _game(pc)
            for g, who in ((ga, "a"), (gb, "b"), (gc, "c")):
                _until(g, "window.room && window.room.media && window.room.me", who + " in")
            ida, idb, idc = (g.evaluate("window.room.me.id") for g in (ga, gb, gc))
            _until(ga, f"window.states[{idb!r}] === 'direct' && window.states[{idc!r}] === 'direct'", "a direct to both")
            ga.evaluate("window.room.media.start({video: true, audio: true})")
            for g in (gb, gc):
                g.evaluate("window.room.media.start({video: false, audio: false})")
            ga.evaluate(f"window.room.media.share({idb!r}, true)")
            ga.evaluate(f"window.room.media.share({idc!r}, true)")
            _until(gb, f"window.has[{ida!r}] && window.has[{ida!r}].video", "b gets a's picture")
            _until(gc, f"window.has[{ida!r}] && window.has[{ida!r}].video", "c gets a's picture")

            # A page with no card is asked, as for the camera; c says no.
            gc.evaluate(f"(() => {{ window.asked = window.room.media.frames({ida!r}, {{w: 320, h: 240, fps: 10}}); }})()")
            pc.wait_for_selector("text=Let this app show the pictures itself?", timeout=10000)
            assert "could also keep them" in pc.inner_text(".askcard"), "the ask says what it lets the app do"
            pc.click(".askcard button.ghost")
            refused = gc.evaluate("window.asked")
            assert refused == {"ok": False, "error": "off"}, refused
            said = gb.evaluate(f"window.room.media.frames({ida!r}, {{w: 320, h: 240, fps: 10}})")
            assert said == {"ok": True}, said
            gb.evaluate(f"window.room.media.hear({ida!r}, true)")
            _until(gb, "window.pics.length >= 8", "b's app gets a's pictures", timeout=20000)
            pics = gb.evaluate("window.pics")
            assert all(k == ida and 0 < w <= 320 and 0 < h <= 240 for k, w, h, t in pics), pics[:3]
            assert pics[-1][3] > pics[0][3], "each frame says when it was taken"
            assert "pictures shown in the app" in pb.inner_text(".arcade-media-pill").lower()
            pc.wait_for_timeout(1500)
            assert gc.evaluate("window.pics.length") == 0, "no picture without the switch"

            # Its sound placed in the app's scene: the viewer's own element goes quiet, the sound plays through the graph.
            gb.evaluate(f"window.room.media.spatial({{listener: {{p: [0, 0, 0], f: [0, 0, -1], u: [0, 1, 0]}},"
                        f" sources: {{[{ida!r}]: {{p: [2, 0, -3], full: 3, end: 16}}}}}})")
            _until(pb, f"(() => {{ var v = document.querySelector('.arcade-media-layer video[data-media={ida}]');"
                       f" return v && v.muted; }})()", "a's sound goes the placed way on b")
            gb.evaluate(f"window.room.media.levels([{ida!r}])")
            _until(gb, f"window.lv && window.lv[{ida!r}] && window.lv[{ida!r}].rgb", "a far screen's colour", timeout=10000)
            gb.evaluate(f"window.room.media.frames({ida!r}, null)")
            gb.evaluate("window.room.media.levels([])")
            n = gb.evaluate("window.pics.length")
            pb.wait_for_timeout(1200)
            assert gb.evaluate("window.pics.length") - n <= 1, "null stops it"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_page_loading_while_another_tab_writes_storage_throws_nothing(tmp_path, no_nodes):
    """2026-11-03, live on app.dogecoinarcade.com: "ReferenceError: ARCADE_MEDIA is not defined". The viewer's storage
    listener ran while the page was still loading, before base.html's script below it had defined its names."""
    state, server, base = _server(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            chrome = _launch(pw)
            ctx = chrome.new_context()
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # Locally the page comes too fast for another tab's write to land mid-load, so the moment is made: a storage
            # event, as another tab's write gives, right before base.html's script (where ARCADE_MEDIA is defined).

            def mid_load(route):
                body = route.fetch().text()
                at = body.rindex("<script", 0, body.index("var ARCADE_MEDIA = "))
                fire = ("<script>window.dispatchEvent(new StorageEvent('storage', {key: 'zz.race', newValue: '1'}));"
                        "window.dispatchEvent(new StorageEvent('storage', {key: 'arcade.media.allow:x', newValue: null}));"
                        "</script>")
                route.fulfill(status=200, content_type="text/html", body=body[:at] + fire + body[at:])
            page.route(f"{base}/inscriptions/{GAME}/full", mid_load)
            page.goto(f"{base}/inscriptions/{GAME}/full")
            page.wait_for_timeout(800)
            assert not [e for e in errors if "is not defined" in e], errors[:3]
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()
