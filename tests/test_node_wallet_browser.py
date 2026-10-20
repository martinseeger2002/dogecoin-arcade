"""The operator's Wallet in a real browser (2026-10-19): the accounts' page, drawn from the node's wallets, and a send
made through it -- Send, Review, Send -- that the regtest node then holds in its pool."""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                       # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_claim_browser import _free_port                          # noqa: E402
from test_end_to_end import chain                                  # noqa: E402,F401
from test_realtime_rejoin_browser import Server, _wait             # noqa: E402
from test_tokens_web import web                                    # noqa: E402,F401


def test_the_operator_sends_from_the_accounts_wallet_page(web):
    app, state, node, alice, bob = web
    port = _free_port()
    srv = Server(app.app, port)
    driver = browsers.launch()
    try:
        driver.get(f"http://127.0.0.1:{port}/wallet")
        _wait(driver, "return document.body.dataset.walletReady === 'yes'"
                      " && document.getElementById('bal').textContent !== '—'", "the balance")
        assert float(driver.find_element(By.ID, "bal").text.replace(",", "")) > 0
        driver.find_element(By.ID, "do-send").click()
        _wait(driver, "return !!document.getElementById('send-amount')", "the send sheet")
        driver.find_element(By.ID, "send-to").send_keys(bob)
        driver.find_element(By.ID, "send-amount").send_keys("2")
        driver.execute_script("document.querySelector('.wl-go').click()")
        _wait(driver, "return [...document.querySelectorAll('.wl-go')].some(b => /^Send 2 /.test(b.textContent))",
              "the review")
        assert "this node's own wallet" in driver.page_source
        assert node.rpc.call("getrawmempool") == [], "the review sends nothing"
        driver.execute_script("[...document.querySelectorAll('.wl-go')].find(b => /^Send 2 /.test(b.textContent)).click()")
        end = time.time() + 30
        while time.time() < end and not node.rpc.call("getrawmempool"):
            time.sleep(0.5)
        assert len(node.rpc.call("getrawmempool")) == 1
    finally:
        driver.quit()
        srv.stop()
