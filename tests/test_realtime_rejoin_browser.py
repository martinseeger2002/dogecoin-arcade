"""A node restarting under two players in a room, in two real browsers.

2026-10-02: every restart of a node (a merge restarts it) dropped every room on
it without a word. The stream reconnected to a node that had never heard of its
token, the browser gave up on it, and the players sat in a room nobody could
hear until they reloaded. The viewer now takes the seat again by itself, as
the same guest, and tells the page who left and who is there.

The restart here is a real one: the server stops, every connection drops, and
a new server with a new mesh (no rooms, no tokens) comes up on the same port.
"""

import pathlib
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                       # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_web import fake_verify, make_state               # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

GAME = "ed" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.log = []; window.room = null;
arcade.realtime.join('plaza', {game: 'rejoin-test'}).then(function (r) {
  window.room = r;
  r.on('message', function (d) { window.log.push({said: d}); });
  r.on('join', function (p) { window.log.push({join: p.id}); });
  r.on('leave', function (p) { window.log.push({leave: p.id}); });
  r.on('closed', function (why) { window.log.push({closed: why}); });
});
</script>"""


def _mesh():
    import nacl.signing

    from arcade.mesh.service import MeshService
    return MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                       listen_host="127.0.0.1", listen_port=0,
                       verify=fake_verify).start()


class Server:
    def __init__(self, app, port):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                    log_level="error", lifespan="off",
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


def _wait(driver, script, what, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        got = driver.execute_script(script)
        if got:
            return got
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}: {driver.execute_script('return window.log')}")


def _in_game(driver, base):
    driver.get(f"{base}/inscriptions/{GAME}/full")
    driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, ".inscription-frame"))


def test_players_come_back_into_their_room_after_the_node_restarts(tmp_path, no_nodes):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    app = create_app(state)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = Server(app, port)
    a = b = None
    try:
        a, b = browsers.launch(), browsers.launch()
        _in_game(a, base)
        _in_game(b, base)
        ids = {}
        for name, d in (("a", a), ("b", b)):
            ids[name] = _wait(d, "return window.room && window.room.online && window.room.me.id",
                              f"{name} in the room")
        _wait(a, "return window.room.members().length === 2", "both in the room")

        # The restart: every connection drops, and the node that comes back has
        # a mesh of its own with no rooms and no tokens in it.
        server.stop()
        state.mesh.stop()
        state.mesh = _mesh()
        server = Server(app, port)

        # Both back, each under the name it had, and hearing each other again.
        end = time.time() + 90
        heard = False
        while time.time() < end and not heard:
            b.execute_script("window.room.send({hi: 1}).catch(function () {});")
            time.sleep(1)
            heard = a.execute_script(
                "return window.log.some(function (e) { return e.said && e.said.hi === 1; })")
        assert heard, a.execute_script("return window.log")
        assert a.execute_script("return window.room.me.id") == ids["a"]
        assert b.execute_script("return window.room.me.id") == ids["b"]
        assert not a.execute_script("return window.log.some(function (e) { return 'closed' in e; })"), \
            "the page is never told the room closed: it came back"
        room = next(iter(state.mesh.node.local))[0]
        assert sorted(m["member"] for m in state.mesh.members(room)) == sorted(ids.values()), \
            "the new node has both players, and nobody twice"
    finally:
        for d in (a, b):
            if d is not None:
                d.quit()
        server.stop()
        state.mesh.stop()
