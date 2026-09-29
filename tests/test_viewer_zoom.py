"""The picture viewer zooms on its own.

A picture in a post used to open as a page of its own, where the phone's pinch
zoomed it. Opened in the viewer, a pinch zoomed the page behind instead (Robin,
2026-09-25). These pin the viewer's own zoom: buttons, wheel, a drag that pans,
and a fresh picture always starting whole.

Skipped when selenium or a browser is unavailable, like the other browser tests.
"""

import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                      # noqa: E402


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The real application, serving the page the viewer lives on."""
    import socket
    import threading
    import time
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    from arcade.messaging.keys import Identity

    state = AppState(
        home=tmp_path_factory.mktemp("zoom"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")),
    )
    state.identity = Identity.generate()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield (f"http://127.0.0.1:{port}",)
    server.should_exit = server.force_exit = True
    thread.join(timeout=5)

# A 400x300 picture, drawn by the page itself so the test needs no file.
PICTURE = """
var c = document.createElement('canvas'); c.width = 400; c.height = 300;
var g = c.getContext('2d'); g.fillStyle = '#c33'; g.fillRect(0, 0, 400, 300);
g.fillStyle = '#33c'; g.fillRect(150, 100, 100, 100);
arcadeOpenImage(c.toDataURL(), 'a picture');
"""


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def _scale(browser) -> float:
    return browser.execute_script("""
        var t = getComputedStyle(document.querySelector('#lightbox img')).transform;
        return t === 'none' ? 1 : new DOMMatrix(t).a;""")


def _opened(browser, served):
    base = served[0]
    browser.get(f"{base}/feed")
    browser.execute_script(PICTURE)
    browser.execute_script("return new Promise(r => { var i = document.querySelector("
                           "'#lightbox img'); i.complete ? r() : i.onload = r; })")


def test_the_buttons_zoom_in_and_out(browser, served):
    _opened(browser, served)
    assert _scale(browser) == 1
    browser.execute_script("document.querySelector('.lightbox-in').click()")
    assert _scale(browser) == pytest.approx(1.5)
    browser.execute_script("document.querySelector('.lightbox-in').click()")
    assert _scale(browser) == pytest.approx(2.25)
    browser.execute_script("document.querySelector('.lightbox-out').click()")
    assert _scale(browser) == pytest.approx(1.5)
    for _ in range(5):
        browser.execute_script("document.querySelector('.lightbox-out').click()")
    assert _scale(browser) == 1, "never smaller than the picture fitted to the screen"


def test_the_wheel_zooms_the_picture_not_the_page(browser, served):
    _opened(browser, served)
    zoomed_page = browser.execute_script("""
        var s = document.querySelector('.lightbox-stage'), r = s.getBoundingClientRect();
        var e = new WheelEvent('wheel', {deltaY: -300, clientX: r.left + r.width / 2,
                                         clientY: r.top + r.height / 2, cancelable: true,
                                         bubbles: true});
        s.dispatchEvent(e);
        return e.defaultPrevented;""")
    assert zoomed_page is True, "the page behind must not take the wheel"
    assert _scale(browser) > 1.5


def test_a_zoomed_picture_can_be_dragged_around(browser, served):
    _opened(browser, served)
    browser.execute_script("for (var i = 0; i < 3; i++) document.querySelector('.lightbox-in').click()")
    before = browser.execute_script("return document.querySelector('#lightbox img').getBoundingClientRect().left")
    browser.execute_script("""
        var img = document.querySelector('#lightbox img'), r = img.getBoundingClientRect();
        var x = r.left + r.width / 2, y = r.top + r.height / 2;
        function p(type, dx) { img.dispatchEvent(new PointerEvent(type, {pointerId: 7,
            clientX: x + dx, clientY: y, bubbles: true, cancelable: true, isPrimary: true})); }
        img.setPointerCapture = function () {};
        p('pointerdown', 0); p('pointermove', 60); p('pointerup', 60);""")
    after = browser.execute_script("return document.querySelector('#lightbox img').getBoundingClientRect().left")
    assert after > before + 30


def test_every_picture_opens_whole(browser, served):
    _opened(browser, served)
    browser.execute_script("document.querySelector('.lightbox-in').click()")
    browser.execute_script("arcadeCloseImage()")
    browser.execute_script(PICTURE)
    assert _scale(browser) == 1
