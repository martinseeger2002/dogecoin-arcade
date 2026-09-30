"""A page asks the arcade around it to inscribe something for the person looking
(2026-09-30, the operator: "inscription requests ... game and data agnostic"), and
optionally to have its result verified by a judge first.

The page is an inscription in a sandboxed frame; everything it asks goes over
postMessage to the viewer, the viewer shows its own card, and the key that signs
is the one in this browser. The template is `test_page_doors_browser.py`, and its
node and helpers are reused.
"""

import base64
import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_doors_browser import (browser, served, _click, _landed,  # noqa: E402,F401
                                     _load_libs, _opened, _settled, PASSWORD)
from selenium.webdriver.common.by import By                           # noqa: E402

JUDGE = """
function judge(seed, inputs, params) {
  var time = 0;
  for (var i = 0; i < inputs.laps.length; i++) time += inputs.laps[i];
  return {won: inputs.laps.length === params.laps, score: time, laps: inputs.laps.length};
}
"""


def _page(judge: str) -> bytes:
    return (
        "<!doctype html><meta charset=utf-8><title>Race</title>"
        "<button id=p>plain</button><button id=j>seed</button><button id=v>verified</button>"
        "<script>"
        "var JUDGE = '" + judge + "';"
        "function ask(m) { return new Promise(function (ok) {"
        "  var seq = Math.random(); m.seq = seq;"
        "  window.addEventListener('message', function h(e) {"
        "    var d = e.data || {};"
        "    if (d.seq === seq && !d.heard) { window.removeEventListener('message', h); ok(d); } });"
        "  parent.postMessage(m, '*'); }); }"
        "document.getElementById('p').onclick = function () {"
        "  ask({arcade: 'inscribe', data: '{\"score\": 1}', contenttype: 'application/json',"
        "       json: {game: 'race-test'}, label: 'Race', note: 'a plain save'})"
        "    .then(function (r) { window.plain = r; }); };"
        "document.getElementById('j').onclick = function () {"
        "  ask({arcade: 'seed', judge: JUDGE}).then(function (r) { window.seeded = r; }); };"
        "document.getElementById('v').onclick = function () {"
        "  ask({arcade: 'inscribe', data: JSON.stringify({track: 'Harbour'}),"
        "       contenttype: 'application/json', json: {game: 'race-test'}, label: 'Race',"
        "       verify: {judge: JUDGE, seed: window.seeded.seed,"
        "                inputs: {laps: [40, 41, 39]}, params: {laps: 3}}})"
        "    .then(function (r) { window.verified = r; }); };"
        "</script>").encode()


def _inscribe_here(browser, state, content: bytes, kind: str, name: str) -> str:
    out = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const wallet = await window.w.opened({network: "regtest", version: arguments[0]});
            const asked = await fetch("/account/inscribe", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({content: arguments[1], content_type: arguments[2],
                                    name: arguments[3], chain: "regtest"})});
            const offer = await asked.json();
            if (!asked.ok) throw new Error(offer.detail || "no offer");
            done({txid: (await window.w.confirm(wallet, offer)).txid});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", state.messaging.params.pubkeyhash_version,
        base64.b64encode(content).decode(), kind, name)
    assert "error" not in out, out
    return out["txid"]


def _yes(browser, timeout=60):
    """Press the card the arcade shows, outside the page's reach."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if browser.execute_script(
                "const b = document.querySelectorAll('.askcard button');"
                "if (!b.length) return false; b[0].click(); return true;"):
            return
        time.sleep(0.3)
    raise AssertionError("the arcade never showed its card")


def test_a_page_inscribes_a_save_and_a_verified_result(browser, served):
    base, state, daemon = served
    _load_libs(browser, base)
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1], {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address}),
                (e) => done({error: String(e.message || e)}));""",
        "racetester", PASSWORD, state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    daemon.rpc.call("sendtoaddress", made["address"], 5.0)
    _opened(state, daemon)
    _load_libs(browser, base)
    judge = _inscribe_here(browser, state, JUDGE.encode(), "text/javascript", "Race judge")
    _landed(state, daemon, judge)
    _load_libs(browser, base)
    page = _inscribe_here(browser, state, _page(judge), "text/html", "Race")
    _landed(state, daemon, page)

    # A new account's first page publishes its messaging key, which spends a
    # coin of its own; let that land before the page spends another.
    browser.get(f"{base}/inscriptions/{page}/view")
    time.sleep(4)
    _opened(state, daemon)
    browser.get(f"{base}/inscriptions/{page}/view")
    frame = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")[0]
    _click(browser, frame, "p")
    _yes(browser)
    plain = _settled(browser, frame, "plain")
    assert plain.get("ok") is True and len(plain.get("txid", "")) == 64, plain

    _click(browser, frame, "j")
    seeded = _settled(browser, frame, "seeded")
    assert len(seeded.get("seed", "")) == 64, seeded
    _click(browser, frame, "v")
    _yes(browser)
    verified = _settled(browser, frame, "verified")
    assert verified.get("ok") is True, verified
    assert verified["result"] == {"won": True, "score": 120, "laps": 3}
    _landed(state, daemon, plain["txid"], verified["txid"])

    import urllib.request
    board = json.loads(urllib.request.urlopen(f"{base}/r/verified/{judge}").read())
    assert [r["id"] for r in board["results"]] == [verified["txid"]], board
    assert board["results"][0]["creatortag"] in ("racetester", None)
    assert board["results"][0]["json"] == {"game": "race-test"}
