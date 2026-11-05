"""An app window stuck loading is started again, in a real browser (2026-11-05: ASHVALE on his iPhone hung on
"Loading Ashvale…" until he removed it from the arcade and added it again: "People should not have to do that").

The home screen's windows are iframes; one whose `load` never comes (a script that never answers, as a request left
hanging by a phone that slept) is loaded afresh by Reload in the pull-down bar, and by itself once when it is still
loading on screen after STUCK_AFTER. A window that loaded is never started again."""
import asyncio
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="the browser half needs selenium")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import browsers                                                  # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(home=tmp_path_factory.mktemp("stuck"),
                     messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                            datadir=Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    app = create_app(state)
    asked = {"hang": 0, "ok": 0}

    @app.get("/zz-hang")
    def hang_page():
        asked["hang"] += 1
        return HTMLResponse('<!doctype html><title>hang</title><p id="boot">Loading…</p><script src="/zz-never.js">'
                            "</script>")

    @app.get("/zz-never.js")
    async def never():
        await asyncio.sleep(600)
        return PlainTextResponse("", media_type="text/javascript")

    @app.get("/zz-ok")
    def ok_page():
        asked["ok"] += 1
        return HTMLResponse('<!doctype html><title>ok</title><p id="ready">Ready</p>')

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", asked
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def _until(check, what, seconds=10.0):
    until = time.time() + seconds
    while time.time() < until:
        try:
            if check():
                return
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(0.25)
    raise AssertionError(what)


def test_a_window_stuck_loading_is_started_again_and_one_that_loaded_is_left_alone(served, browser):
    base, asked = served
    browser.get(f"{base}/home")
    _until(lambda: browser.execute_script("return !!(window.arcadeShell && window.arcadeShell.open)"),
           "the home screen is ready")
    browser.execute_script("arcadeShell.open('/zz-ok', 'Fine app')")
    _until(lambda: asked["ok"] == 1, "the app that loads is asked for")
    browser.execute_script("arcadeShell.open('/zz-hang', 'Stuck app')")
    _until(lambda: asked["hang"] == 1, "the stuck app is asked for")
    time.sleep(1.5)
    # Reload in the pull-down bar: a fresh load of the window in front.
    browser.execute_script("document.getElementById('hs-bar-reload').click()")
    _until(lambda: asked["hang"] == 2, "Reload loads the stuck window afresh")
    shot = pathlib.Path.home() / "ordmock" / "stuck-reload-bar.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    browser.execute_script("var b=document.getElementById('hs-bar');b.hidden=false;"
                           "document.getElementById('hs-bar-name').textContent='ASHVALE';")
    size = browser.get_window_size()
    browser.set_window_size(390, 844)                                 # an iPhone 12's width
    time.sleep(0.5)
    browser.save_screenshot(str(shot))
    browser.set_window_size(size["width"], size["height"])
    # Still loading on screen after STUCK_AFTER (45 s): started again by itself, once, and said so.
    _until(lambda: asked["hang"] == 3, "the window still loading is started again by itself", seconds=60)
    toast = browser.find_element(By.ID, "hs-toast")
    _until(lambda: "was stuck loading" in (toast.get_attribute("textContent") or ""), "the home screen says why it started again", seconds=5)
    assert asked["ok"] == 1, "the window that loaded was never started again"
    # Its second stall is said, not reloaded again: the person picks Reload.
    _until(lambda: "tap Reload" in (toast.get_attribute("textContent") or ""), "a second stall says to tap Reload", seconds=60)
    assert asked["hang"] == 3 and asked["ok"] == 1
