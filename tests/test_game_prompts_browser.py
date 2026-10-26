"""Honest prompts over games, in a real Chromium (2026-10-26, Arcade Hardening round 2, wave 3, issue #7; SadFrog:
"all five"). An inscribed game runs in a sandboxed frame and asks the viewer around it for the wallet; a hostile one
tried to trick the player five ways, one test each:

(a) the camera layer sat above the arcade's cards: it is below them now, and hidden while one is open;
(b) a send request could come up over a different game, under a name it chose: only the framed game's own requests
    are shown, named from the chain;
(c) a page with no game card went straight to the wallet: it is asked first, like a game whose box is off;
(d) Enter or Space meant for the game said yes to a card the player never saw: Cancel has the focus, and the yes
    works only after a moment;
(e) full screen hid everything of the arcade's: a trusted strip stays, outside the game's frame.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server, _mesh             # noqa: E402
from test_realtime_web import make_state                            # noqa: E402
from test_trade_browser import _inscribe                            # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright's Chromium")

MAKER = "n" + "M" * 33
STRANGER = "n" + "S" * 33
GAME, CARD, OTHER_GAME, OTHER_CARD, NO_CARD = "a1" * 32, "a2" * 32, "b1" * 32, "b2" * 32, "c1" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body style="margin:0">
<script src="/r/realtime.js"></script>
<script>
window.room = null; window.said = {};
arcade.realtime.join('call', {game: 'prompt-test', fast: true}).then(function (r) { window.room = r; });
window.addEventListener('message', function (e) { var d = e.data || {}; if (d.seq && !d.heard) window.said[d.seq] = d; });
</script>"""
ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required"]


@pytest.fixture
def arcade(tmp_path, no_nodes):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    state.set_setting("realtime_public_stun", False)
    state.mesh = _mesh()
    _inscribe(state, GAME, 1, MAKER, "text/html", PAGE)
    _inscribe(state, CARD, 2, MAKER, "text/html", b"card", {"game": {"name": "Prompt Game", "play": GAME}})
    _inscribe(state, OTHER_GAME, 3, MAKER, "text/html", PAGE)
    _inscribe(state, OTHER_CARD, 4, MAKER, "text/html", b"card", {"game": {"name": "Other Game", "play": OTHER_GAME}})
    _inscribe(state, NO_CARD, 5, STRANGER, "text/html", PAGE)
    port = _free_port()
    server = Server(create_app(state), port)
    try:
        with sync_api.sync_playwright() as pw:
            try:
                chrome = pw.chromium.launch(args=ARGS)
            except Exception as exc:               # noqa: BLE001
                pytest.skip(f"no Chromium: {exc}")
            yield chrome, f"http://127.0.0.1:{port}"
            chrome.close()
    finally:
        server.stop()
        state.mesh.stop()


def _open(chrome, base, page_id, allow=(), media=(), viewport=None, unreachable=False):
    ctx = chrome.new_context(permissions=["camera", "microphone"],
                             **({"viewport": viewport} if viewport else {}))
    if unreachable:                                  # the node cannot say what the page is
        ctx.route("**/games/*/access", lambda route: route.abort())
    for key in [f"arcade.wallet.allow:{p}" for p in allow] + [f"arcade.media.allow:{p}" for p in media]:
        ctx.add_init_script(f"try {{ localStorage.setItem('{key}', 'yes'); }} catch (e) {{}}")
    page = ctx.new_page()
    page.goto(f"{base}/inscriptions/{page_id}/full")
    page.wait_for_function("typeof arcadeAsk === 'function' && !!document.querySelector('.inscription-frame')")
    return page


def _game(page):
    for f in page.frames:
        if f != page.main_frame and "/content/" in (f.url or ""):
            return f
    raise AssertionError(f"no game frame in {[f.url for f in page.frames]}")


def test_a_the_camera_layer_goes_under_a_card_and_hides_while_it_is_open(arcade):
    chrome, base = arcade
    page = _open(chrome, base, GAME, media=[GAME])
    game = _game(page)
    game.wait_for_function("window.room && window.room.media && window.room.me", timeout=30000)
    said = game.evaluate("window.room.media.start({video: true, audio: false})")
    assert said.get("ok") is True, said
    game.evaluate("window.room.media.place('me', {x: 0, y: 0, w: 400, h: 300})")
    page.wait_for_function("(() => { var v = document.querySelector('.arcade-media-layer video[data-media=me]');"
                           " return v && v.style.display === 'block'; })()", timeout=30000)
    assert page.evaluate("!document.querySelector('.arcade-media-layer').hidden"), "the picture shows over the game"

    page.evaluate("window.answered = null; arcadeAsk('Send 5 coins?', 'To somebody.', 'Yes, send it')"
                  ".then(function (v) { window.answered = v; }); 0")
    page.wait_for_selector(".askcard-back")
    page.wait_for_function("document.querySelector('.arcade-media-layer').hidden", timeout=2000)
    layer, card = page.evaluate("[getComputedStyle(document.querySelector('.arcade-media-layer')).zIndex,"
                                " getComputedStyle(document.querySelector('.askcard-back')).zIndex]")
    assert int(layer) < int(card), (layer, card)
    top = page.evaluate("(() => { var b = document.querySelector('.askcard').getBoundingClientRect();"
                        " var e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);"
                        " return !!e.closest('.askcard'); })()")
    assert top, "the card is on top where it is drawn"
    page.get_by_role("button", name="Cancel").click()
    page.wait_for_function("window.answered === false")
    page.wait_for_function("!document.querySelector('.arcade-media-layer').hidden", timeout=2000)


def test_b_only_the_framed_games_own_send_requests_show_named_from_the_chain(arcade):
    chrome, base = arcade
    page = _open(chrome, base, GAME, allow=[GAME])
    assert page.evaluate(f"[arcadeFramed('{GAME}'), arcadeFramed('{OTHER_GAME}'), arcadeFramed('')]") == \
        [True, False, False]
    who = page.evaluate(f"arcadeRequester('{GAME}')")
    assert who == f"“Prompt Game” (published by {MAKER})", who
    assert page.evaluate(f"arcadeRequester('{NO_CARD}')") == f"A page with no app card (published by {STRANGER})"

    # Two waiting questions for this account, each calling itself "Your Wallet": one filed by another game, one by
    # this one. Only this game's is taken up (its send then fails here: nothing in this test holds a key).
    page.evaluate("""(() => {
      window.ARCADE_ME = 'tester';
      window.arcadeKeys = function () { return Promise.reject(new Error('no key in this test')); };
      window.answers = [];
      var real = window.fetch;
      window.fetch = function (u, o) {
        if (String(u) === '/account/pagesends') return Promise.resolve(new Response(JSON.stringify({chain: 'regtest',
          requests: [{id: 'theirs', page: '%s', kind: 'coins', to: 'nX', amount: '1', label: 'Your Wallet'},
                     {id: 'mine', page: '%s', kind: 'coins', to: 'nX', amount: '1', label: 'Your Wallet'},
                     {id: 'nopage', page: '', kind: 'coins', to: 'nX', amount: '1', label: 'Your Wallet'}]})));
        if (String(u) === '/account/pagesends/answer') {
          window.answers.push(JSON.parse(o.body)); return Promise.resolve(new Response('{"ok": true}'));
        }
        return real(u, o);
      };
    })()""" % (OTHER_GAME, GAME))
    page.wait_for_function("window.answers.length >= 1", timeout=15000)
    page.wait_for_timeout(6000)                      # two more polls
    ids = page.evaluate("window.answers.map(function (a) { return a.id; })")
    assert ids == ["mine"], ids
    assert not page.evaluate("[...document.querySelectorAll('.askcard')].some(c => /Your Wallet/.test(c.textContent))")


def test_c_a_page_with_no_card_is_asked_before_the_wallet(arcade):
    chrome, base = arcade
    page = _open(chrome, base, NO_CARD)
    game = _game(page)
    game.evaluate("parent.postMessage({arcade: 'take', seq: 7}, '*'); 0")
    page.wait_for_selector("text=Allow this app to use your wallet?", timeout=10000)
    assert STRANGER in page.inner_text(".askcard"), "named by who inscribed it"
    page.get_by_role("button", name="Cancel").click()
    game.wait_for_function("window.said[7]", timeout=10000)
    assert "Wallet access is off" in game.evaluate("window.said[7].error")
    assert page.evaluate(f"localStorage.getItem('arcade.wallet.allow:{NO_CARD}')") is None

    # The node could not be asked: still untrusted, still asked.
    fresh = _open(chrome, base, NO_CARD, unreachable=True)
    fresh.evaluate(f"window.open_ = null; arcadeGameGate('{NO_CARD}').then(function (v) {{ window.open_ = v; }}); 0")
    fresh.wait_for_selector("text=Allow this app to use your wallet?", timeout=10000)
    fresh.get_by_role("button", name="Allow wallet access").click()
    fresh.wait_for_function("window.open_ === true")
    assert fresh.evaluate(f"localStorage.getItem('arcade.wallet.allow:{NO_CARD}')") == "yes"

    # A frame the arcade cannot name is asked every time, and never remembered.
    fresh.evaluate("window.blank = null; arcadeGameGate('').then(function (v) { window.blank = v; }); 0")
    fresh.wait_for_selector("text=Allow this page to use your wallet?", timeout=10000)
    fresh.get_by_role("button", name="Cancel").click()
    fresh.wait_for_function("window.blank === false")


def test_d_a_card_over_a_game_focuses_cancel_and_arms_its_yes_after_a_moment(arcade):
    chrome, base = arcade
    page = _open(chrome, base, GAME)
    page.evaluate("window.answered = null; arcadeAsk('Send 5 coins?', 'To somebody.', 'Yes, send it')"
                  ".then(function (v) { window.answered = v; }); 0")
    page.wait_for_selector(".askcard")
    state = page.evaluate("""(() => { var b = [...document.querySelectorAll('.askcard button')];
      return {focus: document.activeElement.textContent, yes: b[0].disabled}; })()""")
    assert state == {"focus": "Cancel", "yes": True}, state
    page.evaluate("document.querySelector('.askcard button').click()")     # pressed at once: lands on nothing
    page.wait_for_timeout(200)
    assert page.evaluate("window.answered") is None
    page.wait_for_function("!document.querySelector('.askcard button').disabled", timeout=3000)
    page.evaluate("document.querySelector('.askcard button').click()")
    page.wait_for_function("window.answered === true")

    # Enter mashed for the game: it lands on Cancel.
    page.evaluate("window.answered = null; arcadeAsk('Send 5 coins?', 'To somebody.', 'Yes, send it')"
                  ".then(function (v) { window.answered = v; }); 0")
    page.wait_for_selector(".askcard")
    page.keyboard.press("Enter")
    page.wait_for_function("window.answered === false")

    # The real-coin review too.
    page.evaluate("window.reviewed = null; arcadeMainnetReview({chain: 'doge', network: 1000, total: 1000})"
                  ".then(function (v) { window.reviewed = v; }); 0")
    page.wait_for_selector(".mainnet-review")
    assert page.evaluate("[document.activeElement.textContent,"
                         " document.querySelector('.mainnet-review button.danger').disabled]") == ["Cancel", True]
    page.wait_for_function("!document.querySelector('.mainnet-review button.danger').disabled", timeout=3000)
    page.click(".mainnet-review button.danger")
    page.wait_for_function("window.reviewed === true")


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 800}, {"width": 360, "height": 740}])
def test_e_full_screen_keeps_a_trusted_strip_the_game_cannot_cover(arcade, viewport):
    chrome, base = arcade
    page = _open(chrome, base, GAME, viewport=viewport)
    strip = page.locator(".trustbar")
    assert strip.is_visible()
    assert "never asks for your password inside a game" in strip.inner_text()
    where = page.evaluate("""(() => {
      var s = document.querySelector('.trustbar').getBoundingClientRect(),
          f = document.querySelector('.inscription-frame').getBoundingClientRect(),
          e = document.elementFromPoint(s.left + s.width / 2, s.top + s.height / 2);
      return {gap: s.top - f.bottom, onTop: e === document.querySelector('.trustbar'),
              inView: s.bottom <= innerHeight + 0.5 && s.top >= 0, whole: s.width >= innerWidth - 0.5,
              cut: document.querySelector('.trustbar').scrollWidth > s.width + 1};
    })()""")
    assert where["gap"] >= -0.5, "the game's frame reaches under the strip: %s" % where
    assert where["onTop"] and where["inView"] and where["whole"] and not where["cut"], where
