"""Any node as a player's way in, in real browsers (2026-10-12, the operator: "I want the client to be able
to access any one of the connected nodes as their game server").

Node A serves the game page; the viewer has picked node B as the quickest way in (set here
directly, as the timing would). The player joins the room THROUGH B -- cross-site, no cookie, no
form token -- and meets a player who opened the game on B itself. Then a viewer whose pick is a
node that does not answer falls back to its own node by itself.

B is `public` here because in real life B is reached through its tunnel, which makes every
request a request from outside; on loopback nothing else would.
"""

import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server, _in_game, _wait  # noqa: E402
from test_realtime_web import fake_verify, make_state               # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

GAME = "ed" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script>
<script>
window.log = []; window.room = null;
arcade.realtime.join('plaza', {game: 'anynode-test'}).then(function (r) {
  window.room = r;
  r.on('message', function (d, p) { window.log.push({said: d, by: p.id}); });
  r.on('join', function (p) { window.log.push({join: p.id}); });
});
</script>"""


def _mesh(peers=None):
    import nacl.signing

    from arcade.mesh.service import MeshService
    return MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_host="127.0.0.1",
                       listen_port=0, verify=fake_verify, peers=peers or []).start()


def _pick(driver, base_a, base_b):
    """What the viewer's timing would have stored: node B is the way in, for the next ten minutes."""
    driver.get(f"{base_a}/")
    driver.execute_script("sessionStorage.setItem('arcade-rt-base-2', arguments[0])",
                          json.dumps({"base": base_b, "at": int(time.time() * 1000)}))


def test_a_player_plays_through_another_node_and_falls_back_when_it_is_gone(tmp_path, no_nodes):
    from arcade.web.app import create_app

    sa, sb = make_state(tmp_path, "a"), make_state(tmp_path, "b")
    sa.mesh = _mesh()
    sb.mesh = _mesh(peers=[("127.0.0.1", sa.mesh.node.listen_port)])
    sb.public = True
    for st in (sa, sb):
        _inscribe(st, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    pa, pb = _free_port(), _free_port()
    base_a, base_b = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    servers = [Server(create_app(sa), pa), Server(create_app(sb), pb)]
    x = y = z = None
    try:
        end = time.time() + 10
        while not (sa.mesh.node.links and sb.mesh.node.links) and time.time() < end:
            time.sleep(0.1)
        x, y = browsers.launch(), browsers.launch()
        _pick(x, base_a, base_b)                       # x: page from A, plays through B
        _in_game(x, base_a)
        _in_game(y, base_b)                            # y: on B itself
        xid = _wait(x, "return window.room && window.room.online && window.room.me.id", "x in the room")
        _wait(y, "return window.room && window.room.online && window.room.me.id", "y in the room")
        room = next(r for r, _ in sb.mesh.node.local)
        assert xid in {m for r, m in sb.mesh.node.local if r == room}, "x's seat is on B, not A"
        assert not any(r == room for r, _ in sa.mesh.node.local), "and nothing of it on A"
        _wait(y, f"return window.room.members().some(function (p) {{ return p.id === {json.dumps(xid)}; }})"
                 f" || window.log.some(function (e) {{ return e.join === {json.dumps(xid)}; }})", "y sees x")
        x.execute_script("window.room.send({hi: 'through B'});")
        _wait(y, "return window.log.some(function (e) { return e.said && e.said.hi === 'through B'; })",
              "y hears x through B")

        # A pick that does not answer: the viewer goes back to its own node by itself.
        z = browsers.launch()
        _pick(z, base_a, f"http://127.0.0.1:{_free_port()}")
        _in_game(z, base_a)
        zid = _wait(z, "return window.room && window.room.online && window.room.me.id", "z in the room via A")
        assert any(m == zid for _, m in sa.mesh.node.local), "z fell back to A"
    finally:
        for d in (x, y, z):
            if d is not None:
                d.quit()
        for s in servers:
            s.stop()
        sb.mesh.stop()
        sa.mesh.stop()


def _mute_streams(app):
    """A road that answers hello and takes the join but whose room stream never says a word (2026-11-02, another node's
    quick tunnel, live: not the join, not a ping, nothing in 45 s)."""
    import asyncio

    async def muted(scope, receive, send):
        # Both streams a room can ride: its own, and the one hub stream (2026-11-04: once hub/attach went through a
        # public host's door, the page rode B's hub, which this left speaking, and the road never looked silent).
        if scope["type"] == "http" and scope["path"] in ("/realtime/stream", "/realtime/hub"):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream"), (b"access-control-allow-origin", b"*")]})
            await send({"type": "http.response.body", "body": b"", "more_body": True})
            while (await receive())["type"] != "http.disconnect":
                await asyncio.sleep(0)
            return
        await app(scope, receive, send)
    return muted


def test_a_way_in_whose_stream_says_nothing_is_left_for_this_node(tmp_path, no_nodes):
    """2026-11-02, a Riffs Lounge: a player whose page had picked such a road showed up for everybody at once and heard
    nobody, so no call ever reached them, and every 15 s of silence they joined again by the same road. Now a road whose
    stream is silent for RT_FIRST is shunned, in every tab, and the page plays through its own node."""
    from arcade.web.app import create_app

    sa, sb = make_state(tmp_path, "a"), make_state(tmp_path, "b")
    sa.mesh = _mesh()
    sb.mesh = _mesh(peers=[("127.0.0.1", sa.mesh.node.listen_port)])
    sb.public = True
    for st in (sa, sb):
        _inscribe(st, GAME, 1, "n" + "G" * 33, "text/html", PAGE)
    pa, pb = _free_port(), _free_port()
    base_a, base_b = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    servers = [Server(create_app(sa), pa), Server(_mute_streams(create_app(sb)), pb)]
    z = None
    try:
        end = time.time() + 10
        while not (sa.mesh.node.links and sb.mesh.node.links) and time.time() < end:
            time.sleep(0.1)
        z = browsers.launch()
        _pick(z, base_a, base_b)
        _in_game(z, base_a)
        _wait(z, "return window.room && window.room.me && window.room.me.id", "z seated (through B, first)")
        zid = z.execute_script("return window.room.me.id")
        end = time.time() + 40
        while time.time() < end and not any(m == zid for _, m in sa.mesh.node.local):
            time.sleep(0.25)
        assert any(m == zid for _, m in sa.mesh.node.local), "z left the silent road for its own node"
        z.switch_to.default_content()                    # the viewer's storage, not the sandboxed game's
        bad = json.loads(z.execute_script("return localStorage.getItem('arcade-rt-bad') || '{}'"))
        assert base_b in bad, bad
        assert z.execute_script("return JSON.parse(sessionStorage.getItem('arcade-rt-base-2') || 'null')") is None \
            or z.execute_script("return JSON.parse(sessionStorage.getItem('arcade-rt-base-2')).base") == "", "B no longer picked"
        time.sleep(20)                                   # and it stays: no more rounds through B
        assert any(m == zid for _, m in sa.mesh.node.local)
        assert not any(m == zid for _, m in sb.mesh.node.local)
    finally:
        if z is not None:
            z.quit()
        for s in servers:
            s.stop()
        sb.mesh.stop()
        sa.mesh.stop()
