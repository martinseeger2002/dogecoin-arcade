"""The fast lane (2026-10-18, docs/hosting-plan.md section 6): two players in a room joined with {fast: true}, in two
real browsers, connected directly over WebRTC. Plain sends and reliable sends both arrive; signaling never reaches
the game; and a pair that cannot connect directly falls back to the room ('room') and still hears each other.

No STUN here (the public one is turned off): two browsers on one machine connect over their own addresses."""

import os
import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium import webdriver                                       # noqa: E402
from selenium.webdriver.firefox.options import Options               # noqa: E402
from selenium.webdriver.firefox.service import Service               # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server, _in_game, _mesh, _wait   # noqa: E402
from test_realtime_web import make_state                            # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

GAME = "ed" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.log = []; window.room = null; window.fastHeard = []; window.states = {};
arcade.realtime.join('arena', {game: 'fast-test', fast: true}).then(function (r) {
  window.room = r;
  r.on('message', function (d) { window.log.push({said: d}); });
  r.fast.on('message', function (d, p) { window.fastHeard.push(d); });
  r.fast.on('peer', function (p, s) { window.states[p.id] = s; window.log.push({peer: s}); });
});
</script>"""


def _browser():
    """Firefox that shows its own addresses to WebRTC (a browser on one machine hides them behind mDNS names
    the other cannot look up)."""
    options = Options()
    options.add_argument("-headless")
    options.set_preference("media.peerconnection.ice.obfuscate_host_addresses", False)
    options.set_preference("media.peerconnection.ice.loopback", True)
    driver_path = os.environ.get("ARCADE_GECKODRIVER") or os.path.expanduser("~/opt/firefox/geckodriver-wrap")
    try:
        return webdriver.Firefox(options=options,
                                 service=Service(executable_path=driver_path) if os.access(driver_path, os.X_OK)
                                 else None)
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"no usable browser: {exc}")


def _two_players(tmp_path):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.set_setting("realtime_public_stun", False)
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    return state, Server(create_app(state), port), f"http://127.0.0.1:{port}"


def test_two_players_connect_directly_and_hear_each_other(tmp_path, no_nodes):
    state, server, base = _two_players(tmp_path)
    a = b = None
    try:
        a, b = _browser(), _browser()
        _in_game(a, base)
        _in_game(b, base)
        ida = _wait(a, "return window.room && window.room.online && window.room.fast && window.room.me.id", "a in")
        idb = _wait(b, "return window.room && window.room.online && window.room.fast && window.room.me.id", "b in")
        _wait(a, "return window.states[arguments[0]] === 'direct'".replace("arguments[0]", repr(idb)), "a direct")
        _wait(b, "return window.states[arguments[0]] === 'direct'".replace("arguments[0]", repr(ida)), "b direct")
        a.execute_script("for (var i = 0; i < 30; i++) window.room.fast.send({pos: i});"
                         "window.room.fast.send({kill: 'b'}, {reliable: true});")
        _wait(b, "return window.fastHeard.some(function (d) { return d.kill === 'b'; })", "the reliable one")
        assert b.execute_script("return window.fastHeard.filter(function (d) { return 'pos' in d; }).length") >= 20, \
            "on one machine nearly every unreliable send arrives"
        b.execute_script("window.room.fast.send('hi a')")
        _wait(a, "return window.fastHeard.indexOf('hi a') >= 0", "a hears b")
        for d in (a, b):
            assert d.execute_script("return window.log.filter(function (e) { return 'said' in e; }).length") == 0, \
                "the signaling never reached the game"
    finally:
        for d in (a, b):
            if d is not None:
                d.quit()
        server.stop()
        state.mesh.stop()


def test_a_pair_that_cannot_connect_directly_plays_through_the_room(tmp_path, no_nodes):
    state, server, base = _two_players(tmp_path)
    a = b = None
    try:
        a, b = _browser(), _browser()
        for d in (a, b):
            d.get(f"{base}/inscriptions/{GAME}/full")
            # This viewer's WebRTC never connects (as behind two strict routers with no relay).
            d.execute_script("RTCPeerConnection.prototype.setRemoteDescription = function () {"
                             " return Promise.reject(new Error('blocked')); };")
            from selenium.webdriver.common.by import By
            d.switch_to.frame(d.find_element(By.CSS_SELECTOR, ".inscription-frame"))
        ida = _wait(a, "return window.room && window.room.online && window.room.fast && window.room.me.id", "a in")
        idb = _wait(b, "return window.room && window.room.online && window.room.fast && window.room.me.id", "b in")
        _wait(a, f"return window.states[{idb!r}] === 'room'", "a falls back", timeout=40)
        _wait(b, f"return window.states[{ida!r}] === 'room'", "b falls back", timeout=40)
        a.execute_script("window.room.fast.send({kill: 'b'}, {reliable: true});"
                         "for (var i = 0; i < 10; i++) window.room.fast.send({pos: i});")
        _wait(b, "return window.fastHeard.some(function (d) { return d.kill === 'b'; })", "the reliable one")
        _wait(b, "return window.fastHeard.some(function (d) { return d.pos === 9; })", "the latest position")
        assert b.execute_script("return window.fastHeard.filter(function (d) { return 'pos' in d; }).length") <= 3, \
            "through the room only the latest positions go"
    finally:
        for d in (a, b):
            if d is not None:
                d.quit()
        server.stop()
        state.mesh.stop()


def test_chrome_and_firefox_connect_directly(tmp_path, no_nodes):
    """The compact descriptions (sdpPack/sdpUnpack in _page_doors) are rebuilt by each browser from the other's:
    Chromium and Firefox both have to accept what the other sends."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright's Chromium")
    state, server, base = _two_players(tmp_path)
    a = None
    try:
        with sync_api.sync_playwright() as pw:
            try:
                chrome = pw.chromium.launch(args=["--disable-features=WebRtcHideLocalIpsWithMdns"])
            except Exception as exc:               # noqa: BLE001
                pytest.skip(f"no Chromium: {exc}")
            page = chrome.new_page()
            page.goto(f"{base}/inscriptions/{GAME}/full")
            frame = page.frame_locator(".inscription-frame")
            a = _browser()
            _in_game(a, base)
            ida = _wait(a, "return window.room && window.room.online && window.room.fast && window.room.me.id", "a in")
            game = next(f for f in page.frames if f != page.main_frame)
            game.wait_for_function("window.room && window.room.online && window.room.fast", timeout=60000)
            idb = game.evaluate("window.room.me.id")
            _wait(a, f"return window.states[{idb!r}] === 'direct'", "firefox direct")
            game.wait_for_function(f"window.states[{ida!r}] === 'direct'", timeout=60000)
            a.execute_script("window.room.fast.send({kill: 'b'}, {reliable: true});")
            game.wait_for_function("window.fastHeard.some(function (d) { return d.kill === 'b'; })", timeout=30000)
            game.evaluate("window.room.fast.send('hi firefox')")
            _wait(a, "return window.fastHeard.indexOf('hi firefox') >= 0", "firefox hears chrome")
            del frame
            chrome.close()
    finally:
        if a is not None:
            a.quit()
        server.stop()
        state.mesh.stop()
