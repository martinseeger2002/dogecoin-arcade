"""The fast lane as a star (2026-10-27, SadFrog's side for an MMO): a host and two players in Chromium. The players
link only with the host (join {fast: true, link: []}, then room.fast.link([host])), never with each other; the host
sends to one player only ({to}) and sends bytes, which arrive as an ArrayBuffer. A player whose link cannot be made
gets what is meant for it, and only that, through the room, bytes included (as base64)."""

import pathlib
import sys
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright's Chromium")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server, _mesh             # noqa: E402
from test_realtime_web import make_state                            # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

GAME = "ae" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.log = []; window.room = null; window.heard = []; window.states = {};
window.start = function (host) {   // the host links with everybody; a player with nobody until it names the host
 arcade.realtime.join('zone', {game: 'star-test', fast: true, link: host ? null : []}).then(function (r) {
  window.room = r;
  r.on('message', function (d) { window.log.push({said: d}); });
  r.fast.on('message', function (d, p) {
    window.heard.push(d instanceof ArrayBuffer ? {bytes: Array.from(new Uint8Array(d)), from: p.id} : {data: d, from: p.id});
  });
  r.fast.on('peer', function (p, s) { window.states[p.id] = s; });
 });
};
</script>"""


def _wait(frame, script, what, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        got = frame.evaluate(script)
        if got:
            return got
        time.sleep(0.4)
    raise AssertionError(f"timed out waiting for {what}: {frame.evaluate('[window.states, window.heard]')}")


def _setup(tmp_path):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.set_setting("realtime_public_stun", False)
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    return state, Server(create_app(state), port), f"http://127.0.0.1:{port}"


def _player(browser, base, who, blocked=False):
    page = browser.new_context().new_page()
    if blocked:   # this viewer's WebRTC never connects (as behind two strict routers with no relay)
        page.add_init_script("RTCPeerConnection.prototype.setRemoteDescription = function () {"
                             " return Promise.reject(new Error('blocked')); };")
    page.goto(f"{base}/inscriptions/{GAME}/full")
    page.wait_for_selector(".inscription-frame")
    frame = None
    end = time.time() + 60
    while frame is None and time.time() < end:
        frame = next((f for f in page.frames if f != page.main_frame), None)
        time.sleep(0.2)
    _wait(frame, "typeof window.start === 'function'", who + " loaded")
    frame.evaluate(f"window.start({'true' if who == 'host' else 'false'})")
    _wait(frame, "window.room && window.room.online && window.room.fast && window.room.me.id", who + " in")
    return page, frame


def _run(tmp_path, blocked_b):
    state, server, base = _setup(tmp_path)
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(args=["--disable-features=WebRtcHideLocalIpsWithMdns"])
            except Exception as exc:               # noqa: BLE001
                pytest.skip(f"no Chromium: {exc}")
            _, h = _player(browser, base, "host")
            _, a = _player(browser, base, "a")
            _, b = _player(browser, base, "b", blocked=blocked_b)
            hid, aid, bid = (f.evaluate("window.room.me.id") for f in (h, a, b))
            for f in (a, b):
                assert f.evaluate(f"window.room.fast.link([{hid!r}]).then(function (r) {{ return r.ok; }})")
            _wait(a, f"window.states[{hid!r}] === 'direct'", "a linked with the host")
            _wait(h, f"window.states[{aid!r}] === 'direct'", "the host linked with a")
            want_b = "room" if blocked_b else "direct"
            _wait(b, f"window.states[{hid!r}] === {want_b!r}", "b and the host", timeout=60)
            _wait(h, f"window.states[{bid!r}] === {want_b!r}", "the host and b", timeout=60)
            time.sleep(2)
            assert a.evaluate(f"window.states[{bid!r}]") in (None, "gone"), "players do not link with each other"
            assert b.evaluate(f"window.states[{aid!r}]") in (None, "gone"), "players do not link with each other"
            # to one player only, reliably; bytes to the other
            h.evaluate(f"window.room.fast.send({{secret: 'for a'}}, {{to: {aid!r}, reliable: true}});"
                       f"window.room.fast.send(new Uint8Array([1, 2, 3, 250]), {{to: [{bid!r}], reliable: true}});"
                       "window.room.fast.send({all: 1}, {reliable: true});")
            _wait(a, "window.heard.some(function (m) { return m.data && m.data.secret === 'for a'; })", "a's own")
            _wait(b, "window.heard.some(function (m) { return m.bytes && m.bytes.join() === '1,2,3,250'; })", "b's bytes")
            _wait(a, "window.heard.some(function (m) { return m.data && m.data.all === 1; })", "a hears the all")
            _wait(b, "window.heard.some(function (m) { return m.data && m.data.all === 1; })", "b hears the all")
            assert not b.evaluate("window.heard.some(function (m) { return m.data && m.data.secret; })"), \
                "what is sent to a never reaches b"
            assert not a.evaluate("window.heard.some(function (m) { return !!m.bytes; })"), \
                "what is sent to b never reaches a"
            # bytes from a player to the host
            a.evaluate("window.room.fast.send(new Uint8Array([9, 8, 7]).buffer, {reliable: true})")
            _wait(h, "window.heard.some(function (m) { return m.bytes && m.bytes.join() === '9,8,7'; })", "the host hears bytes")
            for f in (h, a, b):
                assert f.evaluate("window.log.filter(function (e) { return 'said' in e; }).length") == 0, \
                    "the signaling never reached the game"
            browser.close()
    finally:
        server.stop()
        state.mesh.stop()


def test_a_star_of_one_host_and_two_players(tmp_path, no_nodes):
    _run(tmp_path, blocked_b=False)


def test_a_player_without_a_link_gets_its_own_through_the_room(tmp_path, no_nodes):
    _run(tmp_path, blocked_b=True)
