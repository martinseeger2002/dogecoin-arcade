"""What an inscribed page can actually do, measured in a browser.

Inscribed code is written by strangers and runs in the browser of a wallet that
can spend. Every claim the documentation makes about what it cannot do is
checked here by inscribing a page that TRIES each one and reporting what
happened -- because a security boundary described but never exercised is a
boundary nobody has tested.
"""

import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium import webdriver                                       # noqa: E402

import browsers                                                      # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402
from selenium.webdriver.firefox.options import Options               # noqa: E402

HOSTILE = b"""<!doctype html><meta charset=utf-8>
<body><pre id=out></pre><script>
const out = {};
function say(k, v){ out[k] = v; document.getElementById('out').textContent =
  JSON.stringify(out); }
try { say('cookies', document.cookie === '' ? 'none' : 'READ ' + document.cookie); }
catch (e) { say('cookies', 'blocked'); }
try { say('parent', String(parent.document.title)); }
catch (e) { say('parent', 'blocked'); }
try { top.location = 'https://example.com'; say('topnav', 'ALLOWED'); }
catch (e) { say('topnav', 'blocked'); }
try { localStorage.setItem('x', '1'); say('storage', 'ALLOWED'); }
catch (e) { say('storage', 'blocked'); }
try { document.forms.length; say('script', 'ran'); } catch (e) {}
// /guide rather than /tokens: a page that needs the node can hang waiting for
// one that is not there, and a test must not confuse slow with refused.
fetch('/guide').then(r => r.text()).then(t => say('wallet', 'READ'))
  .catch(e => say('wallet', 'blocked'));
fetch('https://example.com/').then(r => say('outside', 'ALLOWED'))
  .catch(e => say('outside', 'blocked'));
fetch('/r/blockheight').then(r => r.text()).then(t => say('api', 'read ' + t))
  .catch(e => say('api', 'blocked'));
</script></body>"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def inscribed():
    """A real arcade with one hostile HTML inscription indexed in it."""
    import uvicorn

    from arcade import inscriptions as I
    from arcade import payload as P
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.state import Engine, StateDB, install_schema
    from arcade.tx import ArcadeTransaction, EncodingClass
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = Path(tempfile.mkdtemp())
    db = Database(home / "regtest-ledger.sqlite")
    install_schema(db)
    state_db = StateDB(db)
    engine = Engine(state_db, NETWORKS["regtest"])
    txid = f"{1:064x}"
    with state_db.block_context(100, "h", "p", 0, 1, 0):
        engine.process(ArcadeTransaction(
            txid=txid, block_height=100, position=0,
            encoding_class=EncodingClass.B, sender="nMe", reference=None,
            payload=P.AnyData(data=I.plan(HOSTILE, "text/html")[0]).encode(),
            fee=0))
    db.close()

    nowhere = Path("/nonexistent")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=nowhere),
        ledger=ChainContext(network="regtest", role="ledger",
                            label="Regtest", datadir=nowhere))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1",
                                           port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")

    yield f"http://127.0.0.1:{port}", txid
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def viewer(inscribed):
    """The hostile page, loaded in the viewer, with whatever it managed to do."""
    import json

    base, txid = inscribed
    browser = browsers.launch()

    browser.set_window_size(1000, 900)
    browser.get(f"{base}/inscriptions/{txid}/view")
    frames = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
    assert frames, "the viewer did not draw a frame for an HTML inscription"
    sandbox = frames[0].get_attribute("sandbox")

    browser.switch_to.frame(frames[0])
    tried = {}
    for _ in range(40):
        text = browser.find_element(By.ID, "out").text
        tried = json.loads(text) if text else {}
        if len(tried) >= 7:
            break
        time.sleep(0.25)
    browser.switch_to.default_content()
    url = browser.current_url
    browser.quit()
    yield {"sandbox": sandbox, "tried": tried, "url": url}


def test_the_frame_is_sandboxed_without_same_origin(viewer):
    """With allow-same-origin, inscribed code would share this wallet's origin
    and could read every page in it."""
    assert "allow-scripts" in viewer["sandbox"]
    assert "allow-same-origin" not in viewer["sandbox"]
    assert "allow-top-navigation" not in viewer["sandbox"]
    assert "allow-forms" not in viewer["sandbox"]


def test_an_inscribed_page_runs_its_own_script(viewer):
    """It has to: an inscribed page is normally one file with its script in it,
    and a policy that blocked that would make recursion impossible."""
    assert viewer["tried"].get("script") == "ran"


def test_it_cannot_read_the_wallets_cookies(viewer):
    assert viewer["tried"].get("cookies") == "blocked"


def test_it_cannot_reach_the_page_around_it(viewer):
    assert viewer["tried"].get("parent") == "blocked"


def test_it_cannot_navigate_the_window_away(viewer):
    assert viewer["tried"].get("topnav") == "blocked"
    assert viewer["url"].endswith("/view"), "and did not manage it anyway"


def test_it_cannot_keep_anything_between_visits(viewer):
    assert viewer["tried"].get("storage") == "blocked"


def test_it_cannot_read_any_other_page_of_the_wallet(viewer):
    """Every page but the content API says nothing about CORS, so a
    cross-origin read of one cannot see the answer."""
    assert viewer["tried"].get("wallet") == "blocked"


def test_it_cannot_phone_home(viewer):
    """The strongest property here: an inscription cannot tell anybody that you
    looked at it, because it cannot reach anybody."""
    assert viewer["tried"].get("outside") == "blocked"


def test_it_can_read_the_api_it_is_meant_to(viewer):
    """All of that would be pointless if it also could not do its job."""
    assert str(viewer["tried"].get("api", "")).startswith("read "), viewer["tried"]
