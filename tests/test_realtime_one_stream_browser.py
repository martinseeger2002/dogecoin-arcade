"""One stream for all of a page's rooms (2026-11-03, realtime.py /realtime/hub; found by the Garrys mod session for
Spatial World, the operator: "Build it"). Over plain http a browser opens only 6 connections to a site, and a page in 6
rooms -- one EventSource each -- had none left for its sends. On a local http arcade a page now joins 8 rooms, sends
in each, and hears every one."""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from selenium.webdriver.common.by import By                      # noqa: E402
from test_claim_browser import _free_port                       # noqa: E402
from test_realtime_rejoin_browser import Server, _mesh          # noqa: E402
from test_realtime_web import make_state                         # noqa: E402
from test_trade_browser import _inscribe                         # noqa: E402

GAME = "e8" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.heard = {}; window.sent = {}; window.rooms = [];
var names = ['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7'];
Promise.all(names.map(function (n) { return arcade.realtime.join(n, {game: 'one-stream-test'}); })).then(function (rs) {
  window.rooms = rs;
  rs.forEach(function (r, i) {
    r.on('message', function (d) { window.heard[names[i]] = d; });
  });
  window.ready = true;
}).catch(function (e) { window.failed = String(e && e.message || e); });
</script>"""


def test_a_page_in_eight_rooms_on_a_plain_http_arcade_sends_and_hears_in_every_one(tmp_path, no_nodes):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = Server(create_app(state), port)
    a = b = None
    try:
        a, b = browsers.launch(), browsers.launch()
        for d in (a, b):
            d.get(f"{base}/inscriptions/{GAME}/full")
            d.switch_to.frame(d.find_element(By.CSS_SELECTOR, ".inscription-frame"))
        for d in (a, b):
            end = time.time() + 60
            while time.time() < end and not d.execute_script("return window.ready || window.failed"):
                time.sleep(0.3)
            assert d.execute_script("return window.ready"), d.execute_script("return window.failed")
        started = time.time()
        a.execute_script("""window.rooms.forEach(function (r, i) {
          r.send({n: i}).then(function () { window.sent[i] = true; }, function (e) { window.sent[i] = String(e); }); });""")
        end = time.time() + 20
        while time.time() < end and b.execute_script("return Object.keys(window.heard).length") < 8:
            time.sleep(0.3)
        heard = b.execute_script("return window.heard")
        assert len(heard) == 8, heard
        assert a.execute_script("return Object.values(window.sent).filter(function (x) { return x === true; }).length") == 8
        assert time.time() - started < 10, "no send waited for a connection"
    finally:
        for d in (a, b):
            if d is not None:
                d.quit()
        server.stop()
        state.mesh.stop()


QUIET = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.log = [];
Promise.all([arcade.realtime.join('busy', {game: 'quiet-room-test'}),
             arcade.realtime.join('quiet', {game: 'quiet-room-test'})]).then(function (rs) {
  window.busy = rs[0];
  rs[1].on('reconnecting', function () { window.log.push('reconnecting'); });
  window.ready = true;
}).catch(function (e) { window.failed = String(e && e.message || e); });
</script>"""


def test_a_quiet_room_on_a_hub_shared_with_a_busy_room_is_not_taken_for_dead(tmp_path, no_nodes):
    """2026-11-07, found by the GBC session in Pocket Tide Super: the hub says "ping" only when nothing at all came for
    4 s, and a hub message reaches only its own room. A page in a busy room (an ad every 2 s) and a silent one (its
    traffic all on the fast lane) never heard anything in the silent one, so every ~20 s it rejoined and every
    fast-lane link was rebuilt. Any hub traffic now shows the line is alive for every room on it."""
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", QUIET)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = Server(create_app(state), port)
    a = b = None
    try:
        a, b = browsers.launch(), browsers.launch()
        for d in (a, b):
            d.get(f"{base}/inscriptions/{GAME}/full")
            d.switch_to.frame(d.find_element(By.CSS_SELECTOR, ".inscription-frame"))
        for d in (a, b):
            end = time.time() + 60
            while time.time() < end and not d.execute_script("return window.ready || window.failed"):
                time.sleep(0.3)
            assert d.execute_script("return window.ready"), d.execute_script("return window.failed")
        # b talks in the busy room every second, so a's hub is never quiet for 4 s and never pings.
        b.execute_script("setInterval(function () { window.busy.send({t: Date.now()}); }, 1000);")
        time.sleep(30)                      # REALTIME_DEAD is 15 s, checked every 5 s: two rejoins before the fix
        assert a.execute_script("return window.log") == []
    finally:
        for d in (a, b):
            if d is not None:
                d.quit()
        server.stop()
        state.mesh.stop()
