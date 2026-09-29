"""The showcase inscriptions, inscribed and rendered for real.

Two inscriptions, because that is what recursion is: a library on the chain
once, and a page that loads it by id. If this passes, a page inscribed by
anybody can pull in code inscribed by somebody else and read this node -- which
is the whole claim of `docs/inscription-api.md`.
"""

import json
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium import webdriver                                       # noqa: E402

import browsers                                                      # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402
from selenium.webdriver.firefox.options import Options               # noqa: E402

SHOWCASE = Path("examples/showcase")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def showcased(tmp_path_factory):
    """A node with the library inscribed, then the page that uses it."""
    import uvicorn

    from arcade import inscriptions as I
    from arcade import payload as P
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.state import Engine, StateDB, install_schema
    from arcade.tx import ArcadeTransaction, EncodingClass
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("showcase")
    db = Database(home / "regtest-ledger.sqlite")
    install_schema(db)
    state_db = StateDB(db)
    # This node keeps the bytes. A node only stores an inscription's content
    # when it is asked to (D-113); otherwise /content reassembles it from the
    # chain, and there is no chain here to reassemble from. Left as the
    # default, every page below framed a JSON error instead of the inscription
    # -- which is how twenty browser tests, the sandbox guarantees among them,
    # went from testing something to testing nothing without going red in a
    # way anybody read (BOXA pressed on it; D-121).
    engine = Engine(state_db, NETWORKS["regtest"], keep_content=lambda _: True)

    def inscribe(n, content, content_type, json_text=""):
        height = 100 + n * 10
        for index, body in enumerate(I.plan(content, content_type, json_text)):
            with state_db.block_context(height + index, f"h{height + index}",
                                        "p", 0, 1, 0):
                engine.process(ArcadeTransaction(
                    txid=f"{n * 1000 + index:064x}", block_height=height + index,
                    position=0, encoding_class=EncodingClass.B, sender="nMe",
                    reference=None, payload=P.AnyData(data=body).encode(), fee=0))
        return f"{n * 1000:064x}"

    library_id = inscribe(1, (SHOWCASE / "arcade-lib.js").read_bytes(),
                          "application/javascript",
                          '{"name": "arcade-lib", "version": "1.0.0"}')
    # A picture, so the page has something to compose by URL.
    inscribe(2, b"\x89PNG\r\n\x1a\n" + bytes(64), "image/png")
    # The page, with the library's real id in it -- the step that makes it
    # recursion rather than two unrelated files.
    page = (SHOWCASE / "showcase.html").read_text().replace("__LIBRARY__", library_id)
    page_id = inscribe(3, page.encode(), "text/html",
                       '{"name": "API showcase", "uses": "arcade-lib"}')
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
                                           port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")

    yield f"http://127.0.0.1:{port}", library_id, page_id
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


@pytest.fixture(scope="module")
def rendered(showcased):
    base, library_id, page_id = showcased
    browser = browsers.launch()

    browser.set_window_size(1100, 1000)
    browser.get(f"{base}/inscriptions/{page_id}/view")
    frames = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
    assert frames, "the viewer did not frame the showcase"
    browser.switch_to.frame(frames[0])

    def table(name):
        cells = browser.find_elements(By.CSS_SELECTOR, f"#{name} td")
        out, keys = {}, None
        for n in range(0, len(cells) - 1, 2):
            out[cells[n].text.strip()] = cells[n + 1].text.strip()
        return out

    for _ in range(60):
        if table("sandbox"):
            break
        time.sleep(0.3)
    else:
        # Said here rather than eighteen seconds later at the first hard
        # `find_element`. The loop used to exhaust in silence and every
        # assertion below then checked an empty dict, so a page that never ran
        # reported as a missing element and read as a stale selector (D-121).
        raise AssertionError(
            "the framed inscription never ran: "
            + browser.execute_script("return document.body.innerText")[:400])
    result = {name: table(name) for name in
              ("recursion", "self", "chain", "wallet", "sandbox")}
    result["meta"] = browser.find_element(By.ID, "meta").text
    result["thumbs"] = browser.find_elements(By.CSS_SELECTOR, "#thumbs img")
    result["plaque"] = browser.execute_script(
        "const c = document.getElementById('plaque');"
        "return c.getContext('2d').getImageData(0,0,c.width,c.height)"
        ".data.some(v => v !== 0);")
    browser.switch_to.default_content()
    browser.quit()
    yield result, library_id, page_id


def test_the_library_arrives_off_the_chain(rendered):
    """A <script src="/content/<id>"> pulling code inscribed separately. No
    CDN, nothing outside the machine, and it still runs."""
    result, _, _ = rendered
    assert result["recursion"]["loaded"] == "yes", result["recursion"]
    assert result["recursion"]["version"] == "1.0.0"
    assert "plaque" in result["recursion"]["what it gave us"]


def test_the_page_knows_which_inscription_it_is(rendered):
    """From location.pathname alone -- nothing tells it."""
    result, _, page_id = rendered
    assert page_id in result["self"]["id (from location)"]
    assert result["self"]["content type"] == "text/html"
    assert result["self"]["transactions"].isdigit()
    assert len(result["self"]["sha256"]) == 64


def test_it_reads_its_own_immutable_json(rendered):
    result, _, _ = rendered
    assert json.loads(result["meta"]) == {"name": "API showcase",
                                          "uses": "arcade-lib"}


def test_it_reads_the_chain_and_the_wallet(rendered):
    result, _, _ = rendered
    assert "block height" in result["chain"]
    assert result["wallet"], "the wallet section said nothing at all"


def test_it_composes_another_inscription_by_url(rendered):
    """The picture inscribed beside it, pulled in as an ordinary <img src>."""
    result, _, _ = rendered
    assert result["thumbs"], "no inscription was composed into the page"


def test_the_library_drew_something(rendered):
    """Proof the library is not merely loaded but usable: the canvas has
    pixels in it, and only the inscribed library puts them there."""
    result, _, _ = rendered
    assert result["plaque"] is True


def test_the_showcase_demonstrates_its_own_sandbox(rendered):
    """The page tries each thing it should not be able to do and reports.
    Everything but the API must come back blocked."""
    result, _, _ = rendered
    sandbox = result["sandbox"]
    assert sandbox, "the sandbox section never ran"
    for label, outcome in sandbox.items():
        if label.startswith("read /r/"):
            assert "allowed" in outcome, (label, outcome)
        else:
            assert outcome == "blocked", (label, outcome)


def test_the_showcase_files_are_what_gets_inscribed():
    """The placeholder has to be the only thing that changes between the file
    in the repository and the bytes on the chain."""
    page = (SHOWCASE / "showcase.html").read_text()
    assert page.count("__LIBRARY__") >= 1
    assert "http://" not in page and "https://" not in page.replace(
        "https://example.com", ""), "nothing may be loaded from off-chain"


# --- the artwork ---------------------------------------------------------------


@pytest.fixture(scope="module")
def hours(tmp_path_factory):
    """`hours.html` inscribed beside its library, rendered in the viewer."""
    import uvicorn

    from arcade import inscriptions as I
    from arcade import payload as P
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.state import Engine, StateDB, install_schema
    from arcade.tx import ArcadeTransaction, EncodingClass
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("hours")
    db = Database(home / "regtest-ledger.sqlite")
    install_schema(db)
    state_db = StateDB(db)
    # Keeps the bytes, for the same reason the showcase node does (D-121).
    engine = Engine(state_db, NETWORKS["regtest"], keep_content=lambda _: True)

    def inscribe(n, content, content_type, json_text=""):
        height = 100 + n * 10
        for index, body in enumerate(I.plan(content, content_type, json_text)):
            with state_db.block_context(height + index, f"h{height + index}",
                                        "p", 0, 1, 0):
                engine.process(ArcadeTransaction(
                    txid=f"{n * 1000 + index:064x}", block_height=height + index,
                    position=0, encoding_class=EncodingClass.B, sender="nMe",
                    reference=None, payload=P.AnyData(data=body).encode(), fee=0))
        return f"{n * 1000:064x}"

    library_id = inscribe(1, (SHOWCASE / "arcade-lib.js").read_bytes(),
                          "application/javascript")
    page = (SHOWCASE / "hours.html").read_text().replace("__LIBRARY__", library_id)
    page_id = inscribe(2, page.encode(), "text/html", '{"name": "Hours"}')
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
                                           port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    try:
        browser = browsers.launch()
    except BaseException:                 # a skip included: stop the server
        server.should_exit = True
        thread.join(timeout=5)
        raise

    browser.set_window_size(900, 1100)
    browser.get(f"http://127.0.0.1:{port}/inscriptions/{page_id}/view")
    frame = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
    assert frame, "the artwork was not framed"
    browser.switch_to.frame(frame[0])
    time.sleep(3)

    def table(name):
        cells = browser.find_elements(By.CSS_SELECTOR, f"#{name} td")
        return {cells[n].text.strip(): cells[n + 1].text.strip()
                for n in range(0, len(cells) - 1, 2)}

    if not browser.find_elements(By.ID, "hdr"):
        raise AssertionError(
            "the framed artwork never ran: "
            + browser.execute_script("return document.body.innerText")[:400])

    result = {
        "wallet": table("wallet"),
        "self": table("self"),
        "header": browser.find_element(By.ID, "hdr").text,
        "painted": browser.execute_script(
            "const c = document.getElementById('art');"
            "const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "const seen = new Set();"
            "for (let i = 0; i < d.length; i += 400)"
            "  seen.add(d[i] + ',' + d[i+1] + ',' + d[i+2]);"
            "return seen.size;"),
        "background": browser.execute_script(
            "return getComputedStyle(document.body).backgroundColor"),
    }
    browser.switch_to.default_content()
    browser.quit()
    # Stopped after the test rather than before the yield, because this is the
    # one server in the suite whose fixture never joined: `should_exit` was set
    # and the thread was left serving for the rest of the session.
    try:
        yield result
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "the server is still serving a keep-alive connection"


def test_the_artwork_paints_itself(hours):
    """Many colours means a gradient, a sun and a skyline -- not a blank
    canvas and not one flat fill."""
    assert hours["painted"] > 40, hours["painted"]
    assert hours["background"] != "rgba(0, 0, 0, 0)"


def test_it_names_the_chain_it_is_on(hours):
    """One chain. A page showing a balance from the other would be a number
    about a wallet you do not have where you are looking."""
    # The heading is upper-cased by the stylesheet, and `.text` in a browser
    # gives what is on screen rather than what is in the markup.
    assert "regtest" in hours["header"].lower(), hours["header"]
    assert hours["wallet"].get("chain", "").startswith("regtest")


def test_it_shows_what_the_wallet_holds(hours):
    for row in ("@tag", "spendable", "tokens", "inscriptions held", "addresses"):
        assert row in hours["wallet"], (row, hours["wallet"])


def test_it_knows_itself(hours):
    assert hours["self"]["number"] == "#1"
    assert "bytes in" in hours["self"]["size"]
    assert "Hours" in hours["self"]["its JSON"]


def test_the_artwork_loads_nothing_from_anywhere():
    page = (SHOWCASE / "hours.html").read_text()
    assert "http://" not in page and "https://" not in page
    assert page.count("__LIBRARY__") >= 1
