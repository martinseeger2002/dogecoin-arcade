"""Reading back while a model is still typing (2026-10-24, SadFrog: a renter "cannot scroll up to see what it typed
before; it keeps locked in on the active typing of the model").

Both LLM chats -- the Exchange's LLM tab and the corner helper -- moved their list to the bottom on every streamed
piece. They now use base.html's arcadeFollow: the list follows only while the reader is at its bottom; scrolling up
stops following at once, scrolling back down resumes it, and the reader's own message jumps down.

The first test drives arcadeFollow on a real page with a mouse wheel; the second asks the corner helper as a guest, with
a stand-in model streaming a long reply slowly through the arcade, and scrolls up while it types.
"""

import json
import pathlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium.webdriver.common.action_chains import ActionChains     # noqa: E402
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin  # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                      # noqa: E402

LINES = 160          # pieces the stand-in model streams for one reply
PAUSE = 0.04         # seconds between pieces: about six seconds of typing


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Model(BaseHTTPRequestHandler):
    """An OpenAI-compatible stand-in that types a long reply slowly."""

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"data": [{"id": "stand-in", "context_length": 8192}]})
        self._json(404, {"error": {"message": "no"}})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": {"message": "no"}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for n in range(LINES):
                piece = {"choices": [{"index": 0, "delta": {"content": f"Line {n} of a long answer.\n\n"}}]}
                self.wfile.write(b"data: " + json.dumps(piece).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(PAUSE)
            self.wfile.write(b'data: {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 900}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def arcade(tmp_path_factory):
    import uvicorn

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    model = ThreadingHTTPServer(("127.0.0.1", _free_port()), _Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    class _State(AppState):                        # the helper speaks as the node (its own address), as on .25
        @property
        def derived_address(self):
            return "nNodeAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    home = tmp_path_factory.mktemp("llmscroll")
    state = _State(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")))
    state.set_setting("llm", {"on": True, "url": f"http://127.0.0.1:{model.server_address[1]}", "key": "",
                              "models": ["stand-in"], "at_once": 2, "max_out": 4096, "prices": [], "name": "Test",
                              "free_for": [], "helper_ok": None, "helper_off_own": False})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    model.shutdown()
    model.server_close()


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch("--width=1280", "--height=900")
    yield driver
    driver.quit()


def _gap(driver, element) -> float:
    """How far the list's view is from its bottom, in pixels."""
    return driver.execute_script("const l = arguments[0]; return l.scrollHeight - l.scrollTop - l.clientHeight;", element)


def _until(check, seconds=5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return check()


def _wheel_up(driver, element, pixels=600):
    ActionChains(driver).scroll_from_origin(ScrollOrigin.from_element(element), 0, -pixels).perform()


def test_arcade_follow_lets_the_reader_scroll_up_while_text_streams_in(browser, arcade):
    browser.get(arcade + "/apps")
    lst = browser.execute_script("""
        const l = document.createElement('div');
        l.style.cssText = 'position:fixed;left:20px;top:20px;width:400px;height:300px;overflow:auto;z-index:99999;background:#fff';
        for (let n = 0; n < 60; n++) { const p = document.createElement('p'); p.textContent = 'earlier ' + n; l.appendChild(p); }
        document.body.appendChild(l);
        window.__toBottom = arcadeFollow(l); window.__toBottom(true);
        window.__typing = setInterval(() => { const p = document.createElement('p'); p.textContent = 'streamed';
                                              l.appendChild(p); window.__toBottom(); }, 30);
        return l;""")
    time.sleep(1.0)                                                   # about 30 pieces arrive
    assert _gap(browser, lst) < 40, "while the reader is at the bottom, the list follows the text"
    _wheel_up(browser, lst)
    time.sleep(0.2)
    held = browser.execute_script("return arguments[0].scrollTop", lst)
    time.sleep(1.2)                                                   # about 40 more pieces arrive
    assert abs(browser.execute_script("return arguments[0].scrollTop", lst) - held) < 2, \
        "scrolled up, the reader stays where they are while text keeps arriving"
    assert _gap(browser, lst) > 300
    browser.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", lst)   # back to the bottom
    time.sleep(1.0)                                                   # about 30 more pieces arrive
    assert _until(lambda: _gap(browser, lst) < 40, 2), "back at the bottom, it follows again"
    _wheel_up(browser, lst)
    time.sleep(0.3)
    browser.execute_script("window.__toBottom(true)")                  # the reader sends a message of their own
    time.sleep(0.5)
    assert _until(lambda: _gap(browser, lst) < 40, 2), "the reader's own message jumps down and following resumes"
    browser.execute_script("clearInterval(window.__typing)")


def test_corner_helper_reply_can_be_read_back_while_it_types(browser, arcade):
    browser.get(arcade + "/apps")
    browser.execute_script("localStorage.clear()")
    browser.get(arcade + "/apps")
    browser.find_element(By.ID, "arc-bot").click()
    box = browser.find_element(By.ID, "arc-bot-in")
    box.send_keys("Tell me everything, please")
    browser.execute_script("document.getElementById('arc-bot-form').requestSubmit()")
    lst = browser.find_element(By.ID, "arc-bot-msgs")
    deadline = time.time() + 20                                         # the reply fills more than two screens
    while time.time() < deadline and browser.execute_script(
            "const l = arguments[0]; return l.scrollHeight < l.clientHeight * 2.5", lst):
        time.sleep(0.1)
    text = lst.text
    assert "Line 1 of a long answer" in text, text[-300:]
    assert _gap(browser, lst) < 40, "while the reader is at the bottom, the reply is followed"
    _wheel_up(browser, lst, 800)
    time.sleep(0.2)
    held = browser.execute_script("return arguments[0].scrollTop", lst)
    time.sleep(1.5)                                                   # the model goes on typing
    assert "Line" in lst.text and f"Line {LINES - 1} " not in lst.text, "the reply was still streaming"
    assert abs(browser.execute_script("return arguments[0].scrollTop", lst) - held) < 2, \
        "scrolled up mid-reply, the reader is not pulled back down"
    deadline = time.time() + 20
    while time.time() < deadline and f"Line {LINES - 1} " not in lst.text:
        time.sleep(0.2)
    assert f"Line {LINES - 1} " in lst.text, "the whole reply arrived"
