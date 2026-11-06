"""A node module's controls in the Lite window, in a real browser (2026-11-05: "every aspect ... has ui for the whole
process with the ability to also do it in the console"). The window draws each command's manifest form above the
module's page; pressing one runs the line the console would and shows it; an answer's actions become buttons only for
the module's own commands."""
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_web import app_state  # noqa: F401,E402
from test_module_sandbox import _module, needs_sandbox  # noqa: E402
from test_module_host import Chain  # noqa: E402

from arcade import modhost  # noqa: E402
from arcade.lite import commands as cmdlib  # noqa: E402

MODULE_PY = '''
def command_hello(ctx, who="world", times="1"):
    return {"hello": who, "times": int(times), "height": ctx.rpc("main", "getblockcount"),
            "rows": [{"n": 1, "actions": [{"label": "Again", "line": "hello again 2"},
                                          {"label": "Sneak", "line": "sneak data"},
                                          {"label": "Not mine", "line": "listchains"}]}]}
def command_sneak(ctx, what):
    return what
def command_pay(ctx, txid, fee=1000):
    return {}
def page(ctx, method, path, query, body):
    return {"type": "text/html", "body": "<p>the module's own page</p>"}
'''

FORM = {"title": "Greet", "button": "Say hello", "note": "Says hello.",
        "fields": {"who": {"label": "Whom", "type": "choice", "choices": [["you", "You"], ["me", "Me"]]},
                   "times": {"label": "Times", "type": "number", "value": "3"}}}


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture
def served(app_state, tmp_path):
    import uvicorn
    from arcade.web.app import create_app
    app_state.module_host = modhost.Host(app_state.home, Chain())
    folder = _module(tmp_path / "pkg", code=MODULE_PY, change=lambda m: m["commands"]["hello"].update(
        args=["[who]", "[times]"], form=FORM))
    op = cmdlib.Caller(app_state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {folder}")
    cmdlib.run_line(op, "install hello")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(app_state), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    app_state.module_host.stop()


@needs_sandbox
def test_a_modules_form_runs_the_console_line_and_its_own_actions_only(browser, served):
    wait = WebDriverWait(browser, 30, poll_frequency=0.2)
    browser.get(f"{served}/lite#m-hello")
    panel = wait.until(lambda d: [p for p in d.find_elements(By.CSS_SELECTOR, '[data-modpanel="hello"]')
                                  if p.is_displayed() and p.find_elements(By.TAG_NAME, "form")])[0]
    assert panel.find_element(By.TAG_NAME, "legend").text == "Greet"
    assert "Says hello." in panel.text
    assert browser.find_element(By.ID, "mod-hello-times").get_attribute("value") == "3"
    browser.find_element(By.ID, "mod-hello-who").send_keys("Me")
    panel.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
    wait.until(lambda d: "Console: hello me 3" in panel.text and "812" in panel.text)
    buttons = [b.text for b in panel.find_elements(By.CSS_SELECTOR, ".modout button")]
    assert buttons == ["Again", "Sneak"], "the module's own commands, with a form or not; a line not its own, none"
    panel.find_element(By.CSS_SELECTOR, ".modout button").click()
    wait.until(lambda d: "Console: hello again 2" in panel.text)
    frame = browser.find_element(By.CSS_SELECTOR, 'section[data-panel="m-hello"] iframe')
    assert frame.get_attribute("src").endswith("/m/hello/"), "the module's own page stays below its controls"
    # Another tab: the module's panel (a flex column) is hidden, not left on screen pushing the rest away.
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="overview"]').click()
    wait.until(lambda d: not d.find_element(By.CSS_SELECTOR, 'section[data-panel="m-hello"]').is_displayed())
    assert browser.find_element(By.CSS_SELECTOR, 'section[data-panel="overview"]').is_displayed()
